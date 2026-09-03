import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection
from communications.models import Communication, CommunicationTemplate

from .models import CollectionPayment, CollectionsAccountChangeAudit, FundedPayment, FundingMethodRecommendation, Loan, LoanFormula, LoanStateEvent, Payment, WebhookEvent
from .services import LoanService
from .business_calendar import previous_business_day
from .webhooks import _reopen_loan_after_funding_failure
from .zumrails import (
    CollectionService,
    FundingService,
    SettlementService,
    ZumRailsConfigurationError,
    ZumRailsRequestError,
    ZumRailsService,
    add_business_days,
    extract_zum_transaction_fields,
    funding_configuration_ready,
    normalize_zum_status,
    payload_hash,
)


@override_settings(ZUMRAILS_DRY_RUN=True)
class DashboardAnalyticsTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="dashboard-agent@example.com",
            password="password123",
            full_name="Dashboard Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Dash",
            last_name="Customer",
            email="dashboard-customer@example.com",
            phone="4165550101",
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

    def test_dashboard_collection_amounts_are_split_by_processing_and_completed(self):
        report_date = datetime(2026, 8, 13, 12, 0)
        initiated_at = timezone.make_aware(report_date)
        settled_at = timezone.make_aware(datetime(2026, 8, 13, 14, 0))

        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=initiated_at.date(),
            status="scheduled",
        )
        processing_payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=initiated_at.date(),
            status="pending",
        )
        completed_payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("35.00"),
            scheduled_date=initiated_at.date(),
            status="completed",
            processed_at=settled_at,
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=processing_payment,
            amount=Decimal("50.00"),
            status="processing",
            initiated_at=initiated_at,
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=completed_payment,
            amount=Decimal("35.00"),
            status="completed",
            initiated_at=initiated_at,
            settled_at=settled_at,
        )

        response = self.client.get(
            "/api/loans/dashboard/analytics/",
            {"date_from": "2026-08-13", "date_to": "2026-08-13"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            Decimal(response.data["totals"]["processing_collection_payments_amount"]),
            Decimal("50.00"),
        )
        self.assertEqual(
            Decimal(response.data["totals"]["completed_collection_payments_amount"]),
            Decimal("35.00"),
        )
        self.assertEqual(
            Decimal(response.data["totals"]["collected_payments_amount"]),
            Decimal("35.00"),
        )
        self.assertEqual(
            list(response.data["series"]["processing_collection_payments_amount"]),
            [{"date": initiated_at.date(), "value": Decimal("50")}],
        )
        self.assertEqual(
            response.data["series"]["completed_collection_payments_amount"],
            [{"date": settled_at.date(), "value": Decimal("35")}],
        )

    def test_dashboard_defaulted_count_uses_current_loan_status(self):
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "updated_at"])

        response = self.client.get("/api/loans/dashboard/analytics/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["defaulted_loans_count"], 1)
        self.assertEqual(response.data["totals"]["current_defaulted_loans_count"], 1)

    def test_dashboard_quick_summary_uses_live_customer_and_pending_counts(self):
        Customer.objects.create(
            first_name="Idle",
            last_name="Lead",
            email="idle-lead@example.com",
            phone="4165550102",
            province="ON",
            status="collections",
        )
        pending = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="pending",
            is_active=True,
        )
        self.assertEqual(pending.status, "pending")

        response = self.client.get("/api/loans/dashboard/analytics/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["current_customers_count"], 2)
        self.assertEqual(response.data["totals"]["current_active_customers_count"], 1)
        self.assertEqual(response.data["totals"]["current_pending_loans_count"], 1)
        self.assertEqual(response.data["totals"]["current_active_loans_count"], 1)

    def test_dashboard_active_loan_and_customer_counts_ignore_pending_flags(self):
        idle = Customer.objects.create(
            first_name="Lead",
            last_name="Only",
            email="lead-only@example.com",
            phone="4165550103",
            province="ON",
            status="active",
        )
        Loan.objects.create(
            customer=idle,
            principal=Decimal("300.00"),
            fee=Decimal("60.00"),
            total_amount=Decimal("360.00"),
            balance=Decimal("360.00"),
            status="pending",
            is_active=True,
        )
        paid = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("200.00"),
            fee=Decimal("40.00"),
            total_amount=Decimal("240.00"),
            balance=Decimal("0.00"),
            status="paid_off",
            is_active=False,
        )
        self.assertEqual(paid.status, "paid_off")

        response = self.client.get("/api/loans/dashboard/analytics/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["current_active_loans_count"], 1)
        self.assertEqual(response.data["totals"]["current_active_customers_count"], 1)
        self.assertEqual(response.data["totals"]["paid_off_loans_count"], 1)
        self.assertEqual(response.data["totals"]["current_customers_count"], 2)

    def test_dashboard_sent_payments_exclude_unsent_scheduled_rows(self):
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate(),
            status="completed",
            processed_at=timezone.now(),
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("25.00"),
            scheduled_date=timezone.localdate(),
            status="nsf",
            processed_at=timezone.now(),
        )

        response = self.client.get("/api/loans/dashboard/analytics/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["sent_payments_count"], 2)
        self.assertEqual(response.data["totals"]["nsf_payments_count"], 1)
        self.assertEqual(response.data["totals"]["nsf_ratio"], 50.0)


@override_settings(ZUMRAILS_DRY_RUN=True)
class CollectionExportTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="collections-agent@example.com",
            password="password123",
            full_name="Collections Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Riley",
            last_name="Cole",
            email="riley.cole@example.com",
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="active",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("423.39"),
            status="defaulted",
            is_active=False,
            funded_at=timezone.now() - timedelta(days=40),
        )
        self.client.force_authenticate(self.staff)

    def _add_returned_collection(self, *, amount, reason, returned_at, status="returned"):
        return CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal(str(amount)),
            status=status,
            failure_reason=reason,
            initiated_at=returned_at,
            returned_at=returned_at,
        )

    def test_returned_collections_filters_by_returned_date(self):
        inside = timezone.make_aware(datetime(2026, 8, 10, 12, 0))
        outside = timezone.make_aware(datetime(2026, 7, 1, 12, 0))
        matching = self._add_returned_collection(
            amount="176.61",
            reason="EftFailedInsufficientFunds",
            returned_at=inside,
        )
        outside = self._add_returned_collection(
            amount="50.00",
            reason="EftFailedAccountClosed",
            returned_at=outside,
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {matching.id}\n"
                "NSF fee: $50.00"
            ),
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {outside.id}\n"
                "NSF fee: $50.00"
            ),
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("176.61"),
            status="processing",
            initiated_at=inside,
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"date_from": "2026-08-01", "date_to": "2026-08-31", "export": "1"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["id"], str(matching.id))
        self.assertEqual(row["customer_name"], "Riley Cole")
        self.assertEqual(row["customer_email"], "riley.cole@example.com")
        self.assertEqual(row["customer_phone"], "4165552222")
        self.assertEqual(row["reason"], "EftFailedInsufficientFunds")
        self.assertEqual(Decimal(str(row["missed_amount"])), Decimal("176.61"))
        self.assertEqual(Decimal(str(row["balance"])), Decimal("423.39"))
        self.assertTrue(row["returned_at"].startswith("2026-08-10"))
        self.assertEqual(row["customer_source"], "organic")
        self.assertFalse(row["is_arrive"])

    def test_returned_collections_includes_arrive_and_landing_source(self):
        inside = timezone.make_aware(datetime(2026, 8, 10, 12, 0))
        self._add_returned_collection(
            amount="176.61",
            reason="EftFailedInsufficientFunds",
            returned_at=inside,
        )
        arrive_customer = Customer.objects.create(
            first_name="Marvin",
            last_name="Bade",
            email="marvinbade125@gmail.com",
            phone="3067375928",
            phone_normalized="3067375928",
            province="SK",
            status="active",
            source="arrive",
            arrive_application_id="arrive-marvin-1",
        )
        arrive_loan = Loan.objects.create(
            customer=arrive_customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("1023.94"),
            status="defaulted",
            is_active=False,
            funded_at=timezone.now() - timedelta(days=20),
        )
        CollectionPayment.objects.create(
            loan=arrive_loan,
            amount=Decimal("175.25"),
            status="returned",
            failure_reason="EftFailedInsufficientFunds",
            initiated_at=inside,
            returned_at=inside,
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        by_email = {row["customer_email"]: row for row in response.data}
        self.assertEqual(by_email["riley.cole@example.com"]["customer_source"], "organic")
        self.assertFalse(by_email["riley.cole@example.com"]["is_arrive"])
        self.assertEqual(by_email["marvinbade125@gmail.com"]["customer_source"], "arrive")
        self.assertTrue(by_email["marvinbade125@gmail.com"]["is_arrive"])

    def test_returned_collections_applies_nsf_fees_for_arrive_like_landing(self):
        """Arrive In Collections files get the same $50 NSF extras as Landing."""
        inside = timezone.make_aware(datetime(2026, 8, 10, 12, 0))
        arrive_customer = Customer.objects.create(
            first_name="Marvin",
            last_name="Bade",
            email="marvin.nsf@example.com",
            phone="3067375928",
            phone_normalized="3067375928",
            province="SK",
            status="active",
            source="arrive",
            arrive_application_id="arrive-marvin-nsf",
        )
        arrive_loan = Loan.objects.create(
            customer=arrive_customer,
            principal=Decimal("500.00"),
            fee=Decimal("874.44"),
            total_amount=Decimal("1374.44"),
            balance=Decimal("1023.94"),
            status="defaulted",
            is_active=False,
            funded_at=timezone.now() - timedelta(days=20),
        )
        first = Payment.objects.create(
            loan=arrive_loan,
            amount=Decimal("175.25"),
            scheduled_date=timezone.localdate() - timedelta(days=28),
            status="nsf",
            failure_reason="EftFailedInsufficientFunds",
        )
        second = Payment.objects.create(
            loan=arrive_loan,
            amount=Decimal("175.25"),
            scheduled_date=timezone.localdate() - timedelta(days=14),
            status="nsf",
            failure_reason="EftFailedInsufficientFunds",
        )
        remainder = Payment.objects.create(
            loan=arrive_loan,
            amount=Decimal("147.69"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
        )
        CollectionPayment.objects.create(
            loan=arrive_loan,
            payment=first,
            amount=first.amount,
            status="returned",
            failure_reason="EftFailedInsufficientFunds",
            initiated_at=inside,
            returned_at=inside,
        )
        CollectionPayment.objects.create(
            loan=arrive_loan,
            payment=second,
            amount=second.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
            initiated_at=inside,
            returned_at=inside,
        )
        landing_missed = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("176.61"),
            scheduled_date=timezone.localdate() - timedelta(days=7),
            status="nsf",
            failure_reason="EftFailedInsufficientFunds",
        )
        extra_id = uuid.uuid4()
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate() + timedelta(days=21),
            status="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {extra_id}\n"
                "NSF fee: $50.00\n"
                "Extension interest: $0.00"
            ),
        )
        CollectionPayment.objects.create(
            id=extra_id,
            loan=self.loan,
            payment=landing_missed,
            amount=landing_missed.amount,
            status="returned",
            failure_reason="EftFailedInsufficientFunds",
            initiated_at=inside,
            returned_at=inside,
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )
        self.assertEqual(response.status_code, 200, response.data)

        arrive_loan.refresh_from_db()
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        extra_count = arrive_loan.payments.filter(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        ).count()
        self.assertGreater(arrive_loan.balance, Decimal("1023.94"))
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertGreaterEqual(extra_count, 1)
        self.assertTrue(
            all(
                "NSF fee: $50.00" in (row.notes or "")
                for row in arrive_loan.payments.filter(
                    notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                )
            )
        )
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).count(),
            1,
        )
        arrive_row = next(
            row
            for row in response.data
            if row["customer_email"] == "marvin.nsf@example.com"
        )
        self.assertTrue(arrive_row["is_arrive"])
        self.assertGreater(Decimal(str(arrive_row["balance"])), Decimal("1023.94"))

        again = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )
        self.assertEqual(again.status_code, 200, again.data)
        arrive_loan.refresh_from_db()
        self.assertEqual(
            arrive_loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).count(),
            extra_count,
        )

    def test_status_summary_defaulted_counts_failed_collection_payments(self):
        inside = timezone.make_aware(datetime(2026, 8, 10, 12, 0))
        self._add_returned_collection(
            amount="176.61",
            reason="EftFailedInsufficientFunds",
            returned_at=inside,
        )
        self._add_returned_collection(
            amount="147.18",
            reason="EftFailedStopPayment",
            returned_at=inside,
            status="failed",
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            initiated_at=inside,
        )

        summary = self.client.get(
            "/api/loans/status-summary/",
            {"date_from": "2026-08-01", "date_to": "2026-08-31"},
        )
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["defaulted"], 2)
        self.assertEqual(summary.data["active"], 0)

        listed = self.client.get(
            "/api/loans/",
            {
                "status": "defaulted",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data["count"], 0)

    def test_defaulted_collections_export_uses_latest_missed_payment(self):
        older = timezone.make_aware(datetime(2026, 7, 20, 12, 0))
        newer = timezone.make_aware(datetime(2026, 8, 12, 9, 0))
        self._add_returned_collection(
            amount="100.00",
            reason="EftFailedAccountClosed",
            returned_at=older,
        )
        self._add_returned_collection(
            amount="176.61",
            reason="EftFailedStopPayment",
            returned_at=newer,
        )

        response = self.client.get("/api/loans/defaulted-collections/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["customer_name"], "Riley Cole")
        self.assertEqual(row["customer_email"], "riley.cole@example.com")
        self.assertEqual(row["customer_phone"], "4165552222")
        self.assertEqual(row["reason"], "EftFailedStopPayment")
        self.assertEqual(Decimal(str(row["missed_amount"])), Decimal("176.61"))
        self.assertEqual(Decimal(str(row["balance"])), Decimal("423.39"))

    def test_apply_collection_failure_sets_returned_at(self):
        from loans.zumrails import apply_collection_failure

        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("176.61"),
            status="processing",
        )
        apply_collection_failure(
            collection,
            reason="EftFailedInsufficientFunds",
            status="returned",
        )
        collection.refresh_from_db()
        self.assertEqual(collection.status, "returned")
        self.assertIsNotNone(collection.returned_at)

    def test_apply_collection_failure_marks_nsf_for_non_sufficient_funds(self):
        from loans.zumrails import apply_collection_failure

        missed = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("175.25"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=missed,
            amount=missed.amount,
            status="processing",
        )
        apply_collection_failure(
            collection,
            reason="Non-sufficient funds",
            status="failed",
        )
        missed.refresh_from_db()
        self.assertEqual(missed.status, "nsf")
        self.assertEqual(missed.failure_reason, "Non-sufficient funds")
        self.assertTrue(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f"Collection failure id: {collection.id}",
            ).exists()
        )

    def test_returned_collections_includes_processing_with_zum_failed_status(self):
        missed_at = timezone.now() - timedelta(days=11)
        hidden_processing = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("175.00"),
            status="processing",
            zum_status="Failed",
            failure_reason="EftFailedInsufficientFunds",
            processor_transaction_id="zum-failed-processing-1",
            initiated_at=missed_at,
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            initiated_at=missed_at,
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        ids = {row["id"] for row in response.data}
        self.assertIn(str(hidden_processing.id), ids)
        hidden_processing.refresh_from_db()
        self.assertEqual(hidden_processing.status, "failed")
        self.assertIsNotNone(hidden_processing.returned_at)

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_returned_collections_includes_locally_completed_zum_failures(self, mock_get_tx):
        from loans.zumrails import SettlementService

        mock_get_tx.return_value = {
            "Id": "zum-missed-completed-1",
            "TransactionStatus": "Failed",
            "FailedTransactionEvent": "EftFailedStopPayment",
        }
        inside = timezone.make_aware(datetime(2026, 8, 18, 12, 0))
        visible = self._add_returned_collection(
            amount="147.18",
            reason="EftFailedInsufficientFunds",
            returned_at=inside,
        )
        missed = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="completed",
            processor_transaction_id="zum-missed-completed-1",
            initiated_at=timezone.now() - timedelta(days=11),
            settled_at=timezone.now() - timedelta(days=6),
        )

        SettlementService.reconcile_missed_failures()
        missed.refresh_from_db()
        self.assertEqual(missed.status, "failed")
        self.assertEqual(missed.failure_reason, "EftFailedStopPayment")
        mock_get_tx.assert_called()

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        ids = {row["id"] for row in response.data}
        self.assertEqual(ids, {str(visible.id), str(missed.id)})

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_returned_collections_list_does_not_call_zum(self, mock_get_tx):
        mock_get_tx.side_effect = RuntimeError("Zūm must not be called from the list endpoint")
        visible = self._add_returned_collection(
            amount="147.18",
            reason="EftFailedInsufficientFunds",
            returned_at=timezone.now(),
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            status="completed",
            processor_transaction_id="stale-completed-1",
            initiated_at=timezone.now() - timedelta(days=11),
            settled_at=timezone.now() - timedelta(days=6),
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(visible.id))
        mock_get_tx.assert_not_called()

    @patch(
        "loans.views.SettlementService.reconcile_missed_failures",
        side_effect=RuntimeError("heal failed"),
    )
    def test_returned_collections_lists_when_reconcile_raises(self, _mock_reconcile):
        matching = self._add_returned_collection(
            amount="176.61",
            reason="EftFailedInsufficientFunds",
            returned_at=timezone.now(),
        )

        response = self.client.get(
            "/api/loans/returned-collections/",
            {"export": "1"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(matching.id))

    def test_loan_status_display_for_defaulted_is_in_collections(self):
        from loans.models import Loan

        self.assertEqual(dict(Loan.STATUS_CHOICES)["defaulted"], "In Collections")
        self.assertEqual(dict(Loan.STATUS_CHOICES)["stopped"], "Stopped")
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "updated_at"])
        response = self.client.get(f"/api/loans/{self.loan.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "defaulted")
        self.assertEqual(response.data["status_display"], "In Collections")


@override_settings(
    ZUMRAILS_DRY_RUN=True,
    ZUMRAILS_WEBHOOK_SECRET="test-secret",
)
class ZumRailsWorkflowTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="agent@example.com",
            password="password123",
            full_name="Agent User",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="customer@example.com",
            password="password123",
            full_name="Customer User",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Customer",
            last_name="User",
            email="customer@example.com",
            phone="4165551111",
            phone_normalized="4165551111",
            province="ON",
            status="pending",
            onboarding_stage="portal_active",
            banking_verified=True,
            contract_completed=False,
            requested_loan_amount=Decimal("500.00"),
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-1",
            sync_status="synced",
        )
        self.account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-1",
            name="RBC Chequing",
            type="checking",
            balance=Decimal("1000.00"),
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        self.other_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-2",
            name="TD Chequing",
            type="checking",
            balance=Decimal("900.00"),
            transit_number="54321",
            institution_number="004",
            account_number="9876543210",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.account,
            collections_account=self.account,
            is_active=True,
        )
        self.client.force_authenticate(self.staff)
        configure_response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {
                "emt_email": self.customer.email,
                "emt_source": "application",
                "eft_bank_account_id": str(self.account.id),
                "collections_account_id": str(self.account.id),
            },
            format="json",
        )
        self.assertEqual(configure_response.status_code, 200, configure_response.data)
        self.loan.refresh_from_db()

    def sign(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        signature = base64.b64encode(
            hmac.new(b"test-secret", raw, hashlib.sha256).digest()
        ).decode("utf-8")
        return raw, signature

    def post_webhook(self, payload, signature=None):
        raw, computed = self.sign(payload)
        return self.client.post(
            "/api/webhooks/zumrails/",
            data=raw,
            content_type="application/json",
            HTTP_ZUMRAILS_SIGNATURE=signature or computed,
        )

    def test_funding_requires_saved_configuration(self):
        unconfigured = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("300.00"),
            fee=Decimal("60.00"),
            total_amount=Decimal("360.00"),
            balance=Decimal("360.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            is_active=True,
        )

        response = self.client.post(
            f"/api/loans/{unconfigured.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Funding destination required.")

    def test_funding_requires_contract_signature(self):
        self.loan.contract_signed_at = None
        self.loan.save(update_fields=["contract_signed_at", "updated_at"])

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Contract must be signed before funding.")

        options_response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(options_response.status_code, 200)
        self.assertIn("Contract must be signed before funding.", options_response.data["blockers"])

    def test_customer_contract_completed_allows_funding_when_loan_timestamp_missing(self):
        self.loan.contract_signed_at = None
        self.loan.save(update_fields=["contract_signed_at", "updated_at"])
        self.customer.contract_completed = True
        self.customer.save(update_fields=["contract_completed", "updated_at"])

        options_response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(options_response.status_code, 200)
        self.assertNotIn(
            "Contract must be signed before funding.",
            options_response.data["blockers"],
        )

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_configure_funding_saves_destinations(self):
        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["emt_configured"])
        self.assertTrue(response.data["eft_configured"])
        self.assertTrue(response.data["collections_account_configured"])
        self.assertEqual(response.data["blockers"], [])

    def test_funding_options_heals_missing_destinations_from_primary_account(self):
        """UI may show the primary bank while the loan still has no saved destinations."""
        self.loan.bank_account = None
        self.loan.collections_account = None
        self.loan.funding_destination = {}
        self.loan.save(
            update_fields=["bank_account", "collections_account", "funding_destination", "updated_at"]
        )

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.bank_account_id, self.account.id)
        self.assertEqual(self.loan.collections_account_id, self.account.id)
        self.assertTrue(response.data["eft_configured"])
        self.assertTrue(response.data["collections_account_configured"])
        self.assertNotIn("Funding destination required.", response.data["blockers"])
        self.assertNotIn("Collections account required.", response.data["blockers"])

    def test_funding_options_does_not_heal_from_inactive_connection(self):
        """After a new application, deactivated Flinks accounts must not auto-save."""
        self.connection.is_active = False
        self.connection.save(update_fields=["is_active", "updated_at"])
        self.loan.bank_account = None
        self.loan.collections_account = None
        self.loan.funding_destination = {}
        self.loan.status = "ibv_pending"
        self.loan.save(
            update_fields=[
                "bank_account",
                "collections_account",
                "funding_destination",
                "status",
                "updated_at",
            ]
        )

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertIsNone(self.loan.bank_account_id)
        self.assertIsNone(self.loan.collections_account_id)
        self.assertFalse(response.data["eft_configured"])
        self.assertFalse(response.data["collections_account_configured"])

    def test_configure_accounts_allowed_before_approve(self):
        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )

        response = self.client.patch(
            f"/api/loans/{pending_loan.id}/funding/configuration/",
            {
                "eft_bank_account_id": str(self.other_account.id),
                "collections_account_id": str(self.other_account.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending_loan.refresh_from_db()
        self.assertEqual(pending_loan.bank_account_id, self.other_account.id)
        self.assertEqual(pending_loan.collections_account_id, self.other_account.id)
        self.assertEqual(
            pending_loan.funding_destination.get("eft", {}).get("bank_account_id"),
            str(self.other_account.id),
        )

    def test_approve_persists_selected_accounts(self):
        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("350.00"),
            fee=Decimal("70.00"),
            total_amount=Decimal("420.00"),
            balance=Decimal("420.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )

        response = self.client.post(
            f"/api/loans/{pending_loan.id}/approve/",
            {
                "bank_account_id": str(self.other_account.id),
                "collections_account_id": str(self.other_account.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending_loan.refresh_from_db()
        self.assertEqual(pending_loan.status, "pending_funding")
        self.assertEqual(pending_loan.bank_account_id, self.other_account.id)
        self.assertEqual(pending_loan.collections_account_id, self.other_account.id)

    def test_decline_requires_allowed_reason_and_logs_comment(self):
        from activity.models import ActivityHistory

        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("250.00"),
            fee=Decimal("50.00"),
            total_amount=Decimal("300.00"),
            balance=Decimal("300.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )

        bad = self.client.post(
            f"/api/loans/{pending_loan.id}/decline/",
            {"reason": "bankruptcy", "comment": "should fail"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)

        response = self.client.post(
            f"/api/loans/{pending_loan.id}/decline/",
            {
                "reason": "Unacceptable bank",
                "comment": "Void cheque shows unsupported FI",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        pending_loan.refresh_from_db()
        self.assertEqual(pending_loan.status, "human_declined")
        self.assertIn("Unacceptable bank", pending_loan.decline_reason)
        self.assertIn("Void cheque shows unsupported FI", pending_loan.decline_reason)
        self.assertTrue(
            ActivityHistory.objects.filter(
                loan=pending_loan,
                type="comment",
                title="Loan Declined",
                description__contains="Unacceptable bank",
            ).exists()
        )

    def _arrive_unsigned_loan(self, *, status="pending_funding"):
        arrive_user = User.objects.create_user(
            email="arrive-unsigned@example.com",
            password="password123",
            full_name="Arrive Unsigned",
            user_type="customer",
        )
        arrive_customer = Customer.objects.create(
            portal_user=arrive_user,
            first_name="Arrive",
            last_name="Unsigned",
            email="arrive-unsigned@example.com",
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="pending",
            source=Customer.SOURCE_ARRIVE,
            arrive_application_id="arrive-app-unsigned",
            arrive_zum_user_id="zum-unsigned",
        )
        return Loan.objects.create(
            customer=arrive_customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status=status,
            approved_at=timezone.now() if status == "pending_funding" else None,
            is_active=True,
        )

    @patch("accounts.arrive_integration.queue_decision_webhook")
    def test_arrive_unsigned_contract_can_be_cancelled_as_expired(
        self,
        queue_decision_webhook,
    ):
        from accounts.arrive_integration import build_decision_payload
        from activity.models import ActivityHistory

        loan = self._arrive_unsigned_loan()
        response = self.client.post(
            f"/api/loans/{loan.id}/expire-unsigned-contract/",
            {"comment": "No signature after follow-up"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        loan.refresh_from_db()
        self.assertEqual(loan.status, "expired")
        self.assertFalse(loan.is_active)
        self.assertEqual(loan.decline_reason, "expired")
        queue_decision_webhook.assert_called_once_with(loan, "declined")
        self.assertEqual(
            build_decision_payload(loan, decision="declined")["decline_reasons"],
            ["expired"],
        )
        self.assertTrue(
            ActivityHistory.objects.filter(
                loan=loan,
                title="Loan Expired",
                description__contains="expired",
            ).exists()
        )
        self.assertTrue(
            loan.state_events.filter(
                event_type="expired",
                previous_status="pending_funding",
            ).exists()
        )

    @patch("accounts.arrive_integration.queue_decision_webhook")
    def test_arrive_unsigned_pending_signature_can_be_cancelled_as_expired(
        self,
        queue_decision_webhook,
    ):
        loan = self._arrive_unsigned_loan(status="pending_signature")
        response = self.client.post(
            f"/api/loans/{loan.id}/expire-unsigned-contract/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        loan.refresh_from_db()
        self.assertEqual(loan.status, "expired")
        self.assertFalse(loan.is_active)
        queue_decision_webhook.assert_called_once_with(loan, "declined")

    @patch("accounts.arrive_integration.queue_decision_webhook")
    def test_arrive_pending_signature_can_still_be_declined(
        self,
        queue_decision_webhook,
    ):
        loan = self._arrive_unsigned_loan(status="pending_signature")
        response = self.client.post(
            f"/api/loans/{loan.id}/decline/",
            {"reason": "see comments", "comment": "Did not sign"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        loan.refresh_from_db()
        self.assertEqual(loan.status, "human_declined")
        self.assertIn("see comments", loan.decline_reason)
        queue_decision_webhook.assert_called_once_with(loan, "declined")

    def test_landing_unsigned_contract_can_be_cancelled_as_expired(self):
        landing = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="pending_funding",
            approved_at=timezone.now(),
            is_active=True,
        )
        response = self.client.post(
            f"/api/loans/{landing.id}/expire-unsigned-contract/",
            {"comment": "No signature after three days"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        landing.refresh_from_db()
        self.assertEqual(landing.status, "expired")
        self.assertFalse(landing.is_active)
        self.assertEqual(landing.decline_reason, "expired")
        self.assertTrue(
            landing.state_events.filter(
                event_type="expired",
                previous_status="pending_funding",
            ).exists()
        )

    def test_unapproved_pending_signature_cannot_be_cancelled_as_expired(self):
        landing = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="pending_signature",
            is_active=True,
        )
        response = self.client.post(
            f"/api/loans/{landing.id}/expire-unsigned-contract/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        landing.refresh_from_db()
        self.assertEqual(landing.status, "pending_signature")

    def test_signed_arrive_contract_cannot_be_cancelled_as_expired(self):
        loan = self._arrive_unsigned_loan()
        loan.contract_signed_at = timezone.now()
        loan.save(update_fields=["contract_signed_at", "updated_at"])
        response = self.client.post(
            f"/api/loans/{loan.id}/expire-unsigned-contract/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        loan.refresh_from_db()
        self.assertEqual(loan.status, "pending_funding")

    def test_arrive_ibv_pending_cannot_use_unsigned_contract_expire(self):
        loan = self._arrive_unsigned_loan(status="ibv_pending")
        response = self.client.post(
            f"/api/loans/{loan.id}/expire-unsigned-contract/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        loan.refresh_from_db()
        self.assertEqual(loan.status, "ibv_pending")

    @patch("accounts.arrive_integration.queue_decision_webhook")
    def test_landing_unsigned_contract_still_declines_with_existing_reasons(
        self,
        _queue_decision_webhook,
    ):
        landing = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="pending_funding",
            approved_at=timezone.now(),
            is_active=True,
        )
        response = self.client.post(
            f"/api/loans/{landing.id}/decline/",
            {"reason": "see comments", "comment": "Did not sign"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        landing.refresh_from_db()
        self.assertEqual(landing.status, "human_declined")
        self.assertIn("see comments", landing.decline_reason)

    @patch("accounts.arrive_integration.queue_decision_webhook")
    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.services.transaction.on_commit")
    def test_decline_queues_production_denied_template(
        self,
        on_commit,
        send_template_delay,
        _queue_decision_webhook,
    ):
        on_commit.side_effect = lambda callback: callback()
        denied_template = CommunicationTemplate.objects.create(
            name="DENIED",
            type="email",
            trigger="manual",
            subject="Loan application update",
            content="Denied body",
            is_active=True,
        )
        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("250.00"),
            fee=Decimal("50.00"),
            total_amount=Decimal("300.00"),
            balance=Decimal("300.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )

        LoanService.decline_loan(
            loan=pending_loan,
            reason="Unacceptable bank",
            declined_by=self.staff,
            reason_label="Unacceptable bank",
        )

        send_template_delay.assert_called_once_with(
            str(self.customer.id),
            str(denied_template.id),
            str(pending_loan.id),
        )

    @patch("accounts.arrive_integration.queue_decision_webhook")
    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.services.transaction.on_commit")
    def test_decline_does_not_duplicate_existing_deny_template_send(
        self,
        on_commit,
        send_template_delay,
        _queue_decision_webhook,
    ):
        on_commit.side_effect = lambda callback: callback()
        CommunicationTemplate.objects.create(
            name="DENIED",
            type="email",
            trigger="manual",
            subject="Loan application update",
            content="Denied body",
            is_active=True,
        )
        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("250.00"),
            fee=Decimal("50.00"),
            total_amount=Decimal("300.00"),
            balance=Decimal("300.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )
        Communication.objects.create(
            customer=self.customer,
            loan=pending_loan,
            type="email",
            direction="outbound",
            to_address=self.customer.email,
            subject="Loan application update",
            content="Already sent",
            status="sent",
            template_name="Deny Template",
        )

        LoanService.decline_loan(
            loan=pending_loan,
            reason="Unacceptable bank",
            declined_by=self.staff,
            reason_label="Unacceptable bank",
        )

        send_template_delay.assert_not_called()

    def test_staff_actions_write_detailed_activity_history(self):
        from activity.models import ActivityHistory
        from loans.services import LoanService

        pending_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("50.00"),
            total_amount=Decimal("550.00"),
            balance=Decimal("550.00"),
            status="pending",
            bank_account=self.account,
            is_active=True,
        )
        pending_loan.contract_signed_at = timezone.now()
        pending_loan.save(update_fields=["contract_signed_at", "updated_at"])

        LoanService.approve_loan(pending_loan, approved_by=self.staff)
        approve_log = ActivityHistory.objects.filter(
            loan=pending_loan, title="Loan Approved"
        ).latest("created_at")
        self.assertIn("Approved by Agent User", approve_log.description)
        self.assertIn("Status changed from", approve_log.description)
        self.assertEqual(approve_log.created_by, str(self.staff.id))
        # No vague duplicate status-only row for the same transition.
        self.assertEqual(
            ActivityHistory.objects.filter(loan=pending_loan, title="Loan Approved").count(),
            1,
        )

        LoanService.update_approved_amount(
            pending_loan, Decimal("400.00"), user=self.staff
        )
        amount_log = ActivityHistory.objects.filter(
            loan=pending_loan, title="Approved Amount Changed"
        ).latest("created_at")
        self.assertIn("from $500.00 to $400.00", amount_log.description)
        self.assertIn("by Agent User", amount_log.description)

        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            f"/api/loans/{pending_loan.id}/funding/configuration/",
            {
                "eft_bank_account_id": str(self.other_account.id),
                "collections_account_id": str(self.other_account.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        config_log = ActivityHistory.objects.filter(
            loan=pending_loan, title="Funding Configured"
        ).latest("created_at")
        self.assertIn("Funding account changed from", config_log.description)
        self.assertIn("to", config_log.description)
        self.assertIn("by Agent User", config_log.description)

        timeline = self.client.get(
            f"/api/activities/timeline/?customer_id={self.customer.id}"
        )
        self.assertEqual(timeline.status_code, 200)
        titles = [row["title"] for row in timeline.data]
        self.assertIn("Loan Approved", titles)
        self.assertIn("Approved Amount Changed", titles)
        self.assertIn("Funding Configured", titles)
        approved_row = next(row for row in timeline.data if row["title"] == "Loan Approved")
        self.assertEqual(approved_row["loan"], str(pending_loan.id))
        self.assertEqual(approved_row["created_by_name"], "Agent User")

    def test_configure_rejected_after_funding_locked(self):
        # Locks only stick while an active (processing/completed) funding exists.
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="completed",
            processor_transaction_id="locked-config-tx-1",
            completed_at=timezone.now(),
        )
        self.loan.funding_destination_locked_at = timezone.now()
        self.loan.collections_account_locked_at = timezone.now()
        self.loan.save(
            update_fields=[
                "funding_destination_locked_at",
                "collections_account_locked_at",
                "updated_at",
            ]
        )

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {
                "eft_bank_account_id": str(self.other_account.id),
                "collections_account_id": str(self.other_account.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("locked", str(response.data.get("error", "")).lower())

    def test_funding_override_requires_confirmation(self):
        FundingMethodRecommendation.objects.update_or_create(
            weekday=timezone.localtime().weekday(),
            defaults={"method": "eft", "is_active": True},
        )

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "etransfer", "schedule_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "Override confirmation required.")

    def test_collection_failure_marks_linked_payment_failed(self):
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="pending",
        )
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=Decimal("100.00"),
            status="processing",
            processor_transaction_id="collection-fail-1",
            account_snapshot={"id": str(self.account.id)},
        )

        response = self.post_webhook(
            {
                "Type": "TransactionEvent",
                "Data": {
                    "Id": collection.processor_transaction_id,
                    "Event": "EftFailedInsufficientFunds",
                },
            }
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        collection.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(payment.status, "nsf")

    def test_funding_validation_requires_schedule_confirmation(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": False},
            format="json",
        )

        self.assertIn(response.status_code, [400, 403])
        if response.status_code == 400:
            self.assertIn("schedule_confirmed", response.data)

    def test_adjust_schedule_reprices_daily_interest_from_selected_terms(self):
        formula = LoanFormula.objects.create(
            name="Schedule Adjust 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan.formula = formula
        self.loan.fee = Decimal("378.53")
        self.loan.total_amount = Decimal("878.53")
        self.loan.balance = Decimal("878.53")
        self.loan.save(update_fields=["formula", "fee", "total_amount", "balance", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("219.63"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        start_date = timezone.localdate() + timedelta(days=7)
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "weekly",
                "start_date": start_date.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        payments = list(self.loan.payments.filter(status="scheduled").order_by("scheduled_date"))

        self.assertEqual(payments[0].scheduled_date, previous_business_day(start_date))
        self.assertTrue(all(payment.amount <= Decimal("180.00") for payment in payments))
        self.assertEqual(sum((payment.amount for payment in payments), Decimal("0.00")), self.loan.total_amount)
        self.assertGreater(self.loan.fee, Decimal("350.00"))
        self.assertTrue(
            self.loan.state_events.filter(event_type="amount_updated").exists()
        )

    def test_adjust_schedule_on_active_loan_schedules_remaining_balance_only(self):
        formula = LoanFormula.objects.create(
            name="Active Schedule Adjust 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan.status = "active"
        self.loan.formula = formula
        self.loan.fee = Decimal("378.53")
        self.loan.total_amount = Decimal("878.53")
        self.loan.balance = Decimal("778.53")
        self.loan.save(update_fields=["status", "formula", "fee", "total_amount", "balance", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate() - timedelta(days=7),
            status="completed",
            processed_at=timezone.now(),
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("219.63"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        start_date = timezone.localdate() + timedelta(days=7)
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "weekly",
                "start_date": start_date.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        scheduled = list(self.loan.payments.filter(status="scheduled").order_by("scheduled_date"))

        self.assertEqual(self.loan.payments.filter(status="completed").count(), 1)
        self.assertEqual(sum((payment.amount for payment in scheduled), Decimal("0.00")), self.loan.balance)
        self.assertEqual(self.loan.balance, self.loan.total_amount - Decimal("100.00"))
        self.assertTrue(all(payment.amount <= Decimal("180.00") for payment in scheduled))

    def test_adjust_schedule_can_calculate_amount_from_payment_count(self):
        formula = LoanFormula.objects.create(
            name="Count Schedule Adjust 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan.formula = formula
        self.loan.fee = Decimal("378.53")
        self.loan.total_amount = Decimal("878.53")
        self.loan.balance = Decimal("878.53")
        self.loan.save(update_fields=["formula", "fee", "total_amount", "balance", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("219.63"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        start_date = timezone.localdate() + timedelta(days=7)
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "calculation_mode": "number_of_payments",
                "number_of_payments": 6,
                "frequency": "weekly",
                "start_date": start_date.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        scheduled = list(self.loan.payments.filter(status="scheduled").order_by("scheduled_date"))

        self.assertEqual(len(scheduled), 6)
        self.assertEqual(scheduled[0].scheduled_date, previous_business_day(start_date))
        self.assertEqual(sum((payment.amount for payment in scheduled), Decimal("0.00")), self.loan.balance)
        self.assertEqual(scheduled[-1].amount, self.loan.balance - sum((payment.amount for payment in scheduled[:-1]), Decimal("0.00")))
        self.assertTrue(
            self.loan.state_events.filter(
                event_type="amount_updated",
                notes__icontains="calculated payment",
            ).exists()
        )

    def test_update_scheduled_payment_date_and_amount(self):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        new_date = timezone.localdate() + timedelta(days=10)

        response = self.client.patch(
            f"/api/payments/{payment.id}/",
            {
                "scheduled_date": new_date.isoformat(),
                "amount": "125.50",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        payment.refresh_from_db()
        self.assertEqual(payment.original_date, new_date)
        self.assertEqual(payment.scheduled_date, previous_business_day(new_date))
        self.assertEqual(payment.amount, Decimal("125.50"))

    def test_update_scheduled_payment_rebalances_later_installments(self):
        """Ballooning one installment must shrink later scheduled rows (no double $0)."""
        self.loan.total_amount = Decimal("600.00")
        self.loan.balance = Decimal("600.00")
        self.loan.save(update_fields=["total_amount", "balance", "updated_at"])
        first = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        second = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("250.00"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
        )
        third = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("250.00"),
            scheduled_date=timezone.localdate() + timedelta(days=28),
            status="scheduled",
        )

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "300.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        remaining = list(
            self.loan.payments.filter(status="scheduled")
            .exclude(pk=first.pk)
            .order_by("scheduled_date", "created_at", "id")
        )
        self.assertEqual(first.amount, Decimal("300.00"))
        remaining_total = sum((p.amount for p in remaining), Decimal("0.00"))
        self.assertEqual(first.amount + remaining_total, self.loan.total_amount)
        self.assertEqual(remaining_total, Decimal("300.00"))
        self.assertTrue(all(p.amount > 0 for p in remaining))
        # Original trailing 250+250 cannot both remain unchanged after the balloon.
        self.assertFalse(
            Payment.objects.filter(pk=second.pk, amount=Decimal("250.00")).exists()
            and Payment.objects.filter(pk=third.pk, amount=Decimal("250.00")).exists()
        )

    def test_adjust_schedule_rejects_pending_collection_installment(self):
        formula = LoanFormula.objects.create(
            name="Pending Block Schedule 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=4,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan.status = "active"
        self.loan.formula = formula
        self.loan.save(update_fields=["status", "formula", "updated_at"])
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("150.00"),
            scheduled_date=timezone.localdate(),
            status="pending",
        )

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "bi-weekly",
                "start_date": timezone.localdate().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("pending", response.data["error"].lower())
        self.assertEqual(self.loan.payments.filter(status="pending").count(), 1)

    def test_balance_after_matches_table_order_on_same_date(self):
        from loans.serializers import CustomerLoanPaymentSerializer

        self.loan.total_amount = Decimal("400.00")
        self.loan.balance = Decimal("400.00")
        self.loan.save(update_fields=["total_amount", "balance", "updated_at"])
        older = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("300.00"),
            scheduled_date=timezone.localdate(),
            status="pending",
        )
        newer = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        rows = list(self.loan.payments.all())
        self.assertEqual([p.id for p in rows], [older.id, newer.id])

        data = CustomerLoanPaymentSerializer(rows, many=True).data
        by_id = {row["id"]: row for row in data}
        self.assertEqual(Decimal(by_id[str(older.id)]["balance_after"]), Decimal("100.00"))
        self.assertEqual(Decimal(by_id[str(newer.id)]["balance_after"]), Decimal("0.00"))

    def test_update_scheduled_payment_rejects_completed(self):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="completed",
            processed_at=timezone.now(),
        )

        response = self.client.patch(
            f"/api/payments/{payment.id}/",
            {"amount": "90.00"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be edited", response.data["error"].lower())

    def test_defer_scheduled_payment_moves_to_end_and_schedules_fee(self):
        from loans.services import LoanService

        first = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        second = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
        )
        third = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate() + timedelta(days=28),
            status="scheduled",
        )
        original_balance = self.loan.balance
        original_total = self.loan.total_amount
        second_date = second.scheduled_date
        third_date = third.scheduled_date
        # No formula: flat daily interest from fee / planned schedule span.
        expected_interest = LoanService._deferral_extra_interest(self.loan, 14)
        self.assertEqual(expected_interest, Decimal("50.00"))

        response = self.client.post(f"/api/payments/{first.id}/defer/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.loan.refresh_from_db()

        self.assertEqual(second.scheduled_date, second_date)
        self.assertEqual(third.scheduled_date, third_date)
        self.assertEqual(first.scheduled_date, third_date + timedelta(days=14))
        self.assertEqual(first.amount, Decimal("100.00") + expected_interest)
        delta = Decimal("35.00") + expected_interest
        self.assertEqual(self.loan.balance, original_balance + delta)
        self.assertEqual(self.loan.total_amount, original_total + delta)

        fee = self.loan.payments.get(notes__icontains="Deferral fee")
        self.assertEqual(fee.amount, Decimal("35.00"))
        self.assertEqual(fee.scheduled_date, timezone.localdate())
        self.assertEqual(fee.status, "scheduled")
        self.assertEqual(fee.type, "scheduled")
        self.assertEqual(response.data["deferral_fee"]["id"], str(fee.id))

    def test_mark_deferral_fee_paid_via_interac(self):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        defer = self.client.post(f"/api/payments/{payment.id}/defer/", {}, format="json")
        self.assertEqual(defer.status_code, 200, defer.data)
        fee_id = defer.data["deferral_fee"]["id"]
        balance_after_defer = Loan.objects.get(pk=self.loan.pk).balance

        response = self.client.post(
            f"/api/payments/{fee_id}/mark-paid/",
            {"method": "etransfer"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        fee = Payment.objects.get(pk=fee_id)
        self.loan.refresh_from_db()
        self.assertEqual(fee.status, "completed")
        self.assertEqual(fee.type, "etransfer")
        self.assertEqual(self.loan.balance, balance_after_defer - Decimal("35.00"))

    def test_mark_paid_rejects_non_deferral_fee(self):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        response = self.client.post(
            f"/api/payments/{payment.id}/mark-paid/",
            {"method": "manual"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("deferral fee", response.data["error"])

    def test_defer_scheduled_payment_rejects_completed(self):
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="completed",
            processed_at=timezone.now(),
        )

        response = self.client.post(f"/api/payments/{payment.id}/defer/", {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("deferred", response.data["error"])

    def test_stop_and_reactivate_loan(self):
        formula = LoanFormula.objects.create(
            name="Reactivate 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("35.00"),
            default_number_of_payments=8,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan.formula = formula
        self.loan.status = "active"
        self.loan.is_active = True
        self.loan.save(update_fields=["formula", "status", "is_active", "updated_at"])
        scheduled = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        stop = self.client.post(f"/api/loans/{self.loan.id}/mark_defaulted/", {}, format="json")
        self.assertEqual(stop.status_code, 200, stop.data)
        self.loan.refresh_from_db()
        scheduled.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(scheduled.status, "unscheduled")

        missing = self.client.post(f"/api/loans/{self.loan.id}/reactivate/", {}, format="json")
        self.assertEqual(missing.status_code, 400)

        self.loan.status = "defaulted"
        self.loan.save(update_fields=["status", "updated_at"])
        from_collections = self.client.post(
            f"/api/loans/{self.loan.id}/reactivate/",
            {
                "start_date": str(timezone.localdate()),
                "frequency": "bi-weekly",
                "payment_amount": "50.00",
            },
            format="json",
        )
        self.assertEqual(from_collections.status_code, 400)
        self.loan.status = "stopped"
        self.loan.save(update_fields=["status", "updated_at"])

        reactivate = self.client.post(
            f"/api/loans/{self.loan.id}/reactivate/",
            {
                "notes": "Customer agreed to $50 biweekly",
                "start_date": str(timezone.localdate()),
                "frequency": "bi-weekly",
                "payment_amount": "50.00",
            },
            format="json",
        )
        self.assertEqual(reactivate.status_code, 200, reactivate.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertTrue(self.loan.is_active)
        scheduled_rows = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertGreater(len(scheduled_rows), 8)
        self.assertEqual(scheduled_rows[0].amount, Decimal("50.00"))
        self.assertGreater(self.loan.total_amount, Decimal("500.00"))
        self.assertFalse(self.loan.payments.filter(status="unscheduled").exists())

    def test_duplicate_funding_is_blocked(self):
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="processing-1",
        )

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Funding already exists for this loan.", response.data["error"])

    def test_database_constraint_blocks_concurrent_active_funding(self):
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FundedPayment.objects.create(
                    loan=self.loan,
                    amount=self.loan.principal,
                    method="etransfer",
                    status="processing",
                )

    def test_funding_retry_after_failed_creates_new_record(self):
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="failed",
            processor_transaction_id="failed-1",
            failure_reason="EftFailedInsufficientFunds",
        )

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.loan.funded_payments.count(), 2)
        latest = self.loan.funded_payments.order_by("-created_at").first()
        self.assertEqual(latest.status, "completed")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertIsNotNone(self.loan.funded_at)

    def test_funding_initiate_completes_on_create_and_activates_loan(self):
        """Zūm AP create acceptance activates the loan; do not wait for Completed webhook."""
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        funding = self.loan.funded_payments.order_by("-created_at").first()
        self.assertEqual(funding.status, "completed")
        self.assertIsNotNone(funding.processor_transaction_id)
        self.assertIsNotNone(funding.completed_at)
        self.assertEqual(self.loan.status, "active")
        self.assertTrue(self.loan.is_active)
        self.assertIsNotNone(self.loan.funded_at)
        self.assertIsNotNone(self.loan.funding_destination_locked_at)
        self.assertIsNotNone(self.loan.collections_account_locked_at)

        # Activated loans must not linger in the Pending Funding queue.
        listed = self.client.get("/api/loans/", {"status": "pending_funding"})
        self.assertEqual(listed.status_code, 200)
        listed_ids = {row["id"] for row in listed.data["results"]}
        self.assertNotIn(str(self.loan.id), listed_ids)

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsRequestError("request outcome unknown"),
    )
    def test_unknown_submission_is_retained_and_blocks_another_send(self, mock_send):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 502)
        funding = self.loan.funded_payments.get()
        self.assertEqual(funding.status, "processing")
        self.assertIsNone(funding.processor_transaction_id)
        self.assertEqual(funding.failure_reason, "request outcome unknown")

        duplicate = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("Previous funding attempt did not complete at Zūm", duplicate.data["error"])
        self.assertIn("request outcome unknown", duplicate.data["error"])
        self.assertEqual(mock_send.call_count, 1)

        options = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(options.status_code, 200)
        self.assertTrue(options.data["can_release_stuck_funding"])
        self.assertEqual(options.data["active_funding_failure_reason"], "request outcome unknown")

        release = self.client.post(
            f"/api/loans/{self.loan.id}/funding/release-stuck/",
            {},
            format="json",
        )
        self.assertEqual(release.status_code, 200, release.data)
        funding.refresh_from_db()
        self.assertEqual(funding.status, "failed")
        self.assertIn("Released for retry", funding.failure_reason)

        retry = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        # Still mocked to unknown outcome for initiate_transaction — expect 502, not 400 block.
        self.assertEqual(retry.status_code, 502)
        self.assertEqual(mock_send.call_count, 2)

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsRequestError(
            "request rejected",
            outcome_unknown=False,
        ),
    )
    def test_confirmed_api_rejection_marks_attempt_failed_for_retry(self, mock_send):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 502)
        funding = self.loan.funded_payments.get()
        self.assertEqual(funding.status, "failed")

        retry = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(retry.status_code, 502)
        self.assertEqual(self.loan.funded_payments.count(), 2)
        self.assertEqual(mock_send.call_count, 2)

    def test_invalid_webhook_signature_returns_401(self):
        response = self.post_webhook(
            {"Type": "Transaction", "Data": {"Id": "tx-1", "Status": "Completed"}},
            signature="bad-signature",
        )

        self.assertEqual(response.status_code, 401)

    def test_funding_completed_webhook_activates_loan(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="funding-tx-1",
        )

        response = self.post_webhook(
            {"Type": "Transaction", "Data": {"Id": funding.processor_transaction_id, "Status": "Completed"}}
        )

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "completed")
        self.assertEqual(self.loan.status, "active")
        self.assertIsNotNone(self.loan.funded_at)

    def test_funding_completed_heals_loan_stuck_pending_funding(self):
        """Succeeded AP already marked completed locally must still activate the loan."""
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="etransfer",
            status="completed",
            processor_transaction_id="funding-heal-1",
            zum_status="Completed",
            completed_at=timezone.now(),
        )
        self.assertEqual(self.loan.status, "pending_funding")

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertTrue(self.loan.is_active)

    def test_interac_security_question_failure_notifies_and_unlocks(self):
        from activity.models import ActivityHistory

        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="etransfer",
            status="processing",
            processor_transaction_id="interac-sq-1",
            zum_status="InProgress",
        )
        message = (
            "Interac Failed Security Question Needed For Provided Email. "
            "The provided e-mail is not authorized to receive transfers without security question."
        )

        response = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "TransactionStatus": "InProgress",
                "MemberMessage": message,
            },
        })

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "failed")
        self.assertIn("Security Question", funding.failure_reason)
        self.assertEqual(self.loan.status, "pending_funding")
        alert = ActivityHistory.objects.filter(
            loan=self.loan,
            title="Funding Failed",
        ).latest("created_at")
        self.assertTrue(alert.metadata.get("staff_alert"))
        self.assertFalse(alert.metadata.get("is_resolved"))

    def test_funding_failure_alert_resolves_when_new_funding_initiated(self):
        from activity.models import ActivityHistory

        failed = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="failed",
            processor_transaction_id="funding-fail-resolve-1",
            zum_status="Failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        alert = ActivityHistory.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="system",
            title="Funding Failed",
            description=failed.failure_reason,
            created_by="system",
            metadata={
                "staff_alert": True,
                "alert_kind": "funding_failure",
                "failure_reason": failed.failure_reason,
                "funded_payment_id": str(failed.id),
                "is_resolved": False,
            },
        )

        alerts = self.client.get("/api/activities/funding-alerts/")
        self.assertEqual(alerts.status_code, 200)
        self.assertTrue(any(row["id"] == str(alert.id) for row in alerts.data))

        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        alert.refresh_from_db()
        self.assertTrue(alert.metadata.get("is_resolved"))
        # Complete-on-create resolves alerts as funding_completed (same attempt).
        self.assertEqual(alert.metadata.get("resolved_reason"), "funding_completed")

        alerts = self.client.get("/api/activities/funding-alerts/")
        self.assertEqual(alerts.status_code, 200)
        self.assertFalse(any(row["id"] == str(alert.id) for row in alerts.data))

    def test_funding_failure_alert_resolves_when_funding_completes(self):
        from activity.models import ActivityHistory

        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="etransfer",
            status="processing",
            processor_transaction_id="funding-complete-resolve-1",
        )
        alert = ActivityHistory.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="system",
            title="Funding Failed",
            description="Prior attempt failed",
            created_by="system",
            metadata={
                "staff_alert": True,
                "alert_kind": "funding_failure",
                "is_resolved": False,
            },
        )

        response = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "TransactionStatus": "Completed",
            },
        })
        self.assertEqual(response.status_code, 200)

        alert.refresh_from_db()
        self.assertTrue(alert.metadata.get("is_resolved"))
        self.assertEqual(alert.metadata.get("resolved_reason"), "funding_completed")
        alerts = self.client.get("/api/activities/funding-alerts/")
        self.assertFalse(any(row["id"] == str(alert.id) for row in alerts.data))

    def test_funding_completed_transaction_event_activates_loan(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="etransfer",
            status="processing",
            processor_transaction_id="funding-event-complete-1",
        )

        response = self.post_webhook({
            "Type": "TransactionEvent",
            "Data": {
                "Id": funding.processor_transaction_id,
                "Event": "Completed",
                "TransactionStatus": "Completed",
            },
        })

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "completed")
        self.assertEqual(self.loan.status, "active")

    def test_official_root_webhook_shape_is_idempotent(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="funding-root-1",
        )
        payload = {
            "Type": "Transaction",
            "Id": funding.processor_transaction_id,
            "TransactionStatus": "Completed",
        }

        first = self.post_webhook(payload)
        exact_duplicate = self.post_webhook(payload)
        semantic_duplicate = self.post_webhook({
            **payload,
            "UpdatedAt": timezone.now().isoformat(),
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(exact_duplicate.status_code, 200)
        self.assertEqual(semantic_duplicate.status_code, 200)
        self.assertEqual(
            LoanStateEvent.objects.filter(loan=self.loan, event_type="funded").count(),
            1,
        )

    def test_webhook_correlates_attempt_by_client_transaction_id(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
        )

        response = self.post_webhook({
            "Type": "Transaction",
            "Id": "processor-late-response-1",
            "ClientTransactionId": str(funding.id),
            "TransactionStatus": "Completed",
        })

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.assertEqual(funding.processor_transaction_id, "processor-late-response-1")
        self.assertEqual(funding.status, "completed")

    def test_late_funding_return_reopens_loan_and_old_completion_is_ignored(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="funding-return-1",
        )
        completed = self.post_webhook({
            "Type": "Transaction",
            "Id": funding.processor_transaction_id,
            "TransactionStatus": "Completed",
        })
        returned = self.post_webhook({
            "Type": "Transaction",
            "Id": funding.processor_transaction_id,
            "TransactionStatus": "Returned",
            "FailedTransactionEvent": "EftFailedCustomerInitiatedReturnCreditOnly",
        })
        stale_completed = self.post_webhook({
            "Type": "Transaction",
            "Id": funding.processor_transaction_id,
            "TransactionStatus": "Completed",
            "UpdatedAt": timezone.now().isoformat(),
        })

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(stale_completed.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "returned")
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertFalse(self.loan.is_active)
        self.assertIsNone(self.loan.funded_at)

    def test_inprogress_webhook_after_complete_on_create_keeps_loan_active(self):
        """Zūm often reports InProgress after AP create — must not undo local completion."""
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "etransfer", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        funding = self.loan.funded_payments.get()
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertEqual(funding.status, "completed")

        webhook = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "Status": "InProgress",
                "ClientTransactionId": str(funding.id),
            },
        })
        self.assertEqual(webhook.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "completed")
        self.assertEqual(normalize_zum_status(funding.zum_status), "InProgress")
        self.assertEqual(self.loan.status, "active")
        self.assertIsNotNone(self.loan.funded_at)

    def test_failure_webhook_after_complete_on_create_reopens_pending_funding(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        funding = self.loan.funded_payments.get()
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")

        failed = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "Status": "Failed",
                "FailedTransactionEvent": "InteracFailedSecurityQuestion",
                "ClientTransactionId": str(funding.id),
            },
        })
        self.assertEqual(failed.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "failed")
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertFalse(self.loan.is_active)
        self.assertIsNone(self.loan.funded_at)
        self.assertIsNone(self.loan.funding_destination_locked_at)

        retry = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(retry.status_code, 200, retry.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertEqual(self.loan.funded_payments.filter(status="completed").count(), 1)

    def test_funding_failure_webhook_stores_zum_reason_and_allows_retry(self):
        from activity.models import ActivityHistory

        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="funding-fail-reason-1",
        )

        response = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "TransactionStatus": "Failed",
                "FailedTransactionEvent": "EftFailedInsufficientFunds",
            },
        })

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(funding.status, "failed")
        self.assertEqual(funding.failure_reason, "EftFailedInsufficientFunds")
        self.assertEqual(funding.zum_status, "Failed")
        self.assertEqual(self.loan.status, "pending_funding")
        readiness = funding_configuration_ready(self.loan)
        self.assertFalse(readiness["has_active_funding"])
        self.assertNotIn(
            "Funding already exists for this loan.",
            readiness["blockers"],
        )
        alert = ActivityHistory.objects.filter(
            loan=self.loan,
            title="Funding Failed",
        ).latest("created_at")
        self.assertEqual(alert.description, "EftFailedInsufficientFunds")
        self.assertTrue(alert.metadata.get("staff_alert"))
        self.assertEqual(alert.metadata.get("alert_kind"), "funding_failure")

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_funding_options_syncs_failed_zum_status_and_unlocks_retry(self, mock_get_tx):
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="662cec6c-f516-45ee-adad-c9fb660cf558",
        )
        mock_get_tx.return_value = {
            "Id": "662cec6c-f516-45ee-adad-c9fb660cf558",
            "TransactionStatus": "Failed",
            "FailedTransactionEvent": "EftFailedAccountClosed",
        }

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["has_active_funding"])
        self.assertEqual(
            self.loan.funded_payments.get().failure_reason,
            "EftFailedAccountClosed",
        )
        self.assertEqual(self.loan.funded_payments.get().status, "failed")
        mock_get_tx.assert_called_once_with("662cec6c-f516-45ee-adad-c9fb660cf558")

    def test_pending_funding_list_excludes_loans_with_active_funding(self):
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="in-progress-tx-1",
            zum_status="InProgress",
        )
        still_needs_funding = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("200.00"),
            fee=Decimal("40.00"),
            total_amount=Decimal("240.00"),
            balance=Decimal("240.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.account,
            collections_account=self.account,
        )

        listed = self.client.get("/api/loans/", {"status": "pending_funding"})
        self.assertEqual(listed.status_code, 200)
        listed_ids = {row["id"] for row in listed.data["results"]}
        self.assertNotIn(str(self.loan.id), listed_ids)
        self.assertIn(str(still_needs_funding.id), listed_ids)

        summary = self.client.get("/api/loans/status-summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["pending_funding"], 1)
        self.assertEqual(summary.data["approved_pending_signature"], 0)

    def test_pending_funding_can_filter_signed_and_unsigned_approved_loans(self):
        unsigned_approved = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("300.00"),
            fee=Decimal("60.00"),
            total_amount=Decimal("360.00"),
            balance=Decimal("360.00"),
            status="pending_funding",
            contract_signed_at=None,
            bank_account=self.account,
            collections_account=self.account,
        )

        signed_response = self.client.get(
            "/api/loans/",
            {"status": "pending_funding", "contract_signed": "true"},
        )
        self.assertEqual(signed_response.status_code, 200)
        signed_ids = {row["id"] for row in signed_response.data["results"]}
        self.assertIn(str(self.loan.id), signed_ids)
        self.assertNotIn(str(unsigned_approved.id), signed_ids)

        unsigned_response = self.client.get(
            "/api/loans/",
            {"status": "pending_funding", "contract_signed": "false"},
        )
        self.assertEqual(unsigned_response.status_code, 200)
        unsigned_ids = {row["id"] for row in unsigned_response.data["results"]}
        self.assertNotIn(str(self.loan.id), unsigned_ids)
        self.assertIn(str(unsigned_approved.id), unsigned_ids)

        summary = self.client.get("/api/loans/status-summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["pending_funding"], 1)
        self.assertEqual(summary.data["approved_pending_signature"], 1)

    def test_funding_failure_list_alerts_and_retry_filter(self):
        from activity.models import ActivityHistory

        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="failed",
            processor_transaction_id="funding-fail-list-1",
            zum_status="Failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        ActivityHistory.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="system",
            title="Funding Failed",
            description="EftFailedInsufficientFunds",
            created_by="system",
            metadata={
                "staff_alert": True,
                "alert_kind": "funding_failure",
                "failure_reason": "EftFailedInsufficientFunds",
            },
        )
        clean_pending = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("200.00"),
            fee=Decimal("40.00"),
            total_amount=Decimal("240.00"),
            balance=Decimal("240.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.account,
            collections_account=self.account,
        )

        listed = self.client.get("/api/loans/", {"needs_funding_retry": "true"})
        self.assertEqual(listed.status_code, 200)
        listed_ids = {row["id"] for row in listed.data["results"]}
        self.assertIn(str(self.loan.id), listed_ids)
        self.assertNotIn(str(clean_pending.id), listed_ids)

        failed_row = next(
            row for row in listed.data["results"] if row["id"] == str(self.loan.id)
        )
        self.assertTrue(failed_row["has_funding_failure"])
        self.assertEqual(failed_row["funding_failure_reason"], "EftFailedInsufficientFunds")

        summary = self.client.get("/api/loans/status-summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["funding_failed"], 1)
        self.assertEqual(summary.data["pending_funding"], 2)

        alerts = self.client.get("/api/activities/funding-alerts/")
        self.assertEqual(alerts.status_code, 200)
        self.assertGreaterEqual(len(alerts.data), 1)
        self.assertEqual(alerts.data[0]["title"], "Funding Failed")
        self.assertEqual(alerts.data[0]["description"], "EftFailedInsufficientFunds")
        self.assertEqual(
            alerts.data[0]["metadata"].get("failure_reason"),
            "EftFailedInsufficientFunds",
        )

    def test_funding_cancelled_webhook_unlocks_fund_customer(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="662cec6c-f516-45ee-adad-c9fb660cf558",
        )

        response = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": funding.processor_transaction_id,
                "TransactionStatus": "Cancelled",
            },
        })

        self.assertEqual(response.status_code, 200)
        funding.refresh_from_db()
        self.assertEqual(funding.status, "cancelled")
        self.assertEqual(funding.zum_status, "Cancelled")
        readiness = funding_configuration_ready(self.loan)
        self.assertFalse(readiness["has_active_funding"])
        self.assertNotIn(
            "Funding already exists for this loan.",
            readiness["blockers"],
        )

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_funding_options_heals_stale_cancelled_zum_status(self, mock_get_tx):
        """zum_status Cancelled but status still processing must unlock retry."""
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="662cec6c-f516-45ee-adad-c9fb660cf558",
            zum_status="Cancelled",
            failure_reason="Cancelled",
        )

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["has_active_funding"])
        funding = self.loan.funded_payments.get()
        self.assertEqual(funding.status, "cancelled")
        mock_get_tx.assert_not_called()

    def test_documented_failure_event_outside_legacy_list_is_processed(self):
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            processor_transaction_id="collection-new-failure",
            account_snapshot={"id": str(self.account.id)},
        )

        response = self.post_webhook({
            "Type": "TransactionEvent",
            "Id": collection.processor_transaction_id,
            "Event": "EftFailedFundsNotFree",
            "CreatedAt": timezone.now().isoformat(),
        })

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.event_history[0]["event"], "EftFailedFundsNotFree")
        self.assertEqual(self.loan.status, "defaulted")
        self.assertEqual(self.loan.balance, Decimal("650.00"))
        fee = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            notes__contains=f"Collection failure id: {collection.id}",
        )
        self.assertEqual(fee.amount, Decimal("50.00"))
        self.assertTrue(LoanService.is_collection_failure_fee_payment(fee))

    def test_collection_completed_webhook_keeps_original_settlement_window(self):
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            status="processing",
            processor_transaction_id="collection-tx-1",
            initiated_at=timezone.now() - timedelta(days=1),
            account_snapshot={"id": str(self.account.id)},
        )

        response = self.post_webhook(
            {"Type": "Transaction", "Data": {"Id": collection.processor_transaction_id, "Status": "Completed"}}
        )

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        self.assertEqual(collection.zum_status, "Completed")
        self.assertEqual(collection.status, "processing")
        self.assertIsNotNone(collection.settlement_due_at)
        self.assertLess(collection.settlement_due_at, add_business_days(timezone.now(), 4))

        collection.settlement_due_at = timezone.now() - timedelta(minutes=1)
        collection.save(update_fields=["settlement_due_at"])
        response = self.client.post("/api/loans/settlement/process/", {}, format="json")

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "completed")
        self.assertEqual(self.loan.balance, Decimal("500.00"))

    def test_scheduled_collection_task_does_not_send_payment_twice(self):
        from loans.tasks import process_scheduled_payments

        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        first = process_scheduled_payments()
        second = process_scheduled_payments()

        payment.refresh_from_db()
        self.assertEqual(first["initiated"], 1)
        self.assertEqual(second["initiated"], 0)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.collection_attempts.count(), 1)

    def test_defaulted_loan_still_sends_scheduled_collections(self):
        from loans.tasks import process_scheduled_payments

        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "updated_at"])
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )

        result = process_scheduled_payments()

        payment.refresh_from_db()
        self.assertEqual(result["initiated"], 1)
        self.assertEqual(payment.status, "pending")

    def test_stopped_loan_does_not_send_unscheduled_or_scheduled_collections(self):
        from loans.tasks import process_scheduled_payments

        self.loan.status = "stopped"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "updated_at"])
        unscheduled = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="unscheduled",
        )

        result = process_scheduled_payments()

        unscheduled.refresh_from_db()
        self.assertEqual(result["initiated"], 0)
        self.assertEqual(unscheduled.status, "unscheduled")
        self.assertEqual(unscheduled.collection_attempts.count(), 0)

    def test_collections_account_change_requires_failed_collection(self):
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/collections-account/",
            {"bank_account_id": str(self.other_account.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("failed_payment_id", response.data)

        failed = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            status="failed",
            processor_transaction_id="failed-collection-1",
            failure_reason="EftFailedInsufficientFunds",
            account_snapshot={"id": str(self.account.id)},
        )
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/collections-account/",
            {
                "bank_account_id": str(self.other_account.id),
                "failed_payment_id": str(failed.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.collections_account_id, self.other_account.id)
        audit = CollectionsAccountChangeAudit.objects.get(loan=self.loan)
        self.assertEqual(audit.failed_payment_id, failed.id)
        self.assertEqual(audit.failure_reason, "EftFailedInsufficientFunds")

    def test_collections_account_change_allowed_for_any_eft_failure(self):
        failure_codes = [
            "EftFailedInsufficientFunds",
            "EftFailedAccountClosed",
            "EftFailedCannotLocateAccount",
            "EftFailedStopPayment",
            "EftFailedNoDebitAllowed",
            "EftFailedFrozenAccount",
            "EftFailedInvalidErrorAccountNumber",
            "EftFailedRefusedNoAgreement",
            "EftFailedAgreementRevoked",
            "EftFailedPayorPayeeDeceased",
            "EftFailedNotInAccountAgreementP",
            "EftFailedNotInAccountAgreementE",
            "EftFailedNoPrenotificationP1",
            "EftFailedNoPrenotificationP2",
            "EftFailedDefaultByAFinancialInstitution",
            "EftFailedTransactionLimitExceeded",
            "EftFailedValidationRejection",
            "EftFailedTransactionNotAllowed",
        ]

        for index, failure_reason in enumerate(failure_codes):
            with self.subTest(failure_reason=failure_reason):
                failed = CollectionPayment.objects.create(
                    loan=self.loan,
                    amount=Decimal("10.00") + index,
                    status="failed",
                    processor_transaction_id=f"failed-collection-{index}",
                    failure_reason=failure_reason,
                    account_snapshot={"id": str(self.account.id)},
                )
                target_account = self.other_account if index % 2 else self.account
                response = self.client.patch(
                    f"/api/loans/{self.loan.id}/collections-account/",
                    {
                        "bank_account_id": str(target_account.id),
                        "failed_payment_id": str(failed.id),
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 200, response.data)
                audit = CollectionsAccountChangeAudit.objects.get(failed_payment=failed)
                self.assertEqual(audit.failure_reason, failure_reason)

    def test_process_collection_settlements_task_completes_without_completed_webhook(self):
        from loans.tasks import process_collection_settlements

        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            processor_transaction_id="settlement-task-1",
            settlement_due_at=timezone.now() - timedelta(minutes=1),
            account_snapshot={"id": str(self.account.id)},
        )

        result = process_collection_settlements()

        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(result["completed"], 1)
        self.assertEqual(collection.status, "completed")
        self.assertEqual(self.loan.balance, Decimal("550.00"))

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_process_collection_settlements_applies_zum_failure_instead_of_completing(
        self, mock_get_tx
    ):
        from loans.tasks import process_collection_settlements

        mock_get_tx.return_value = {
            "Id": "settlement-failed-1",
            "TransactionStatus": "Failed",
            "FailedTransactionEvent": "EftFailedInsufficientFunds",
        }
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            processor_transaction_id="settlement-failed-1",
            settlement_due_at=timezone.now() - timedelta(minutes=1),
            account_snapshot={"id": str(self.account.id)},
        )

        result = process_collection_settlements()

        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(result["completed"], 0)
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.failure_reason, "EftFailedInsufficientFunds")
        self.assertEqual(self.loan.status, "defaulted")
        self.assertEqual(self.loan.balance, Decimal("650.00"))

    def test_process_collection_settlements_backfills_missing_due_date(self):
        from loans.tasks import process_collection_settlements

        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            processor_transaction_id="settlement-task-legacy",
            initiated_at=timezone.now() - timedelta(days=7),
            account_snapshot={"id": str(self.account.id)},
        )

        result = process_collection_settlements()

        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(result["completed"], 1)
        self.assertIsNotNone(collection.settlement_due_at)
        self.assertEqual(collection.status, "completed")
        self.assertEqual(self.loan.balance, Decimal("550.00"))

    def test_late_eft_failure_reverses_a_settled_collection_once(self):
        self.loan.status = "active"
        self.loan.balance = Decimal("500.00")
        self.loan.save(update_fields=["status", "balance", "updated_at"])
        payment = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="completed",
        )
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=Decimal("100.00"),
            status="completed",
            processor_transaction_id="late-failure-1",
            zum_status="Completed",
            settled_at=timezone.now(),
            account_snapshot={"id": str(self.account.id)},
        )
        payload = {
            "Type": "TransactionEvent",
            "Id": collection.processor_transaction_id,
            "Event": "EftFailedInsufficientFunds",
        }

        first = self.post_webhook(payload)
        duplicate = self.post_webhook(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        collection.refresh_from_db()
        payment.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(payment.status, "nsf")
        self.assertEqual(self.loan.status, "defaulted")
        self.assertEqual(self.loan.balance, Decimal("650.00"))
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f"Collection failure id: {collection.id}"
            ).count(),
            1,
        )

    def test_extract_prefers_nsf_history_over_completed_status(self):
        fields = extract_zum_transaction_fields({
            "Id": "0737cdbf-6c4c-4990-be61-c48b3fce78f7",
            "TransactionStatus": "Completed",
            "TransactionHistory": [
                {"Event": "Succeeded"},
                {"Event": "EftFailedInsufficientFunds"},
            ],
        })
        self.assertEqual(fields["status"], "Failed")
        self.assertEqual(fields["reason"], "EftFailedInsufficientFunds")

    def _settled_collection(self, processor_transaction_id, amount="147.18"):
        amount = Decimal(amount)
        self.loan.status = "active"
        self.loan.balance = Decimal("500.00")
        self.loan.save(update_fields=["status", "balance", "updated_at"])
        payment = Payment.objects.create(
            loan=self.loan,
            amount=amount,
            scheduled_date=timezone.localdate(),
            status="completed",
            processed_at=timezone.now(),
            reference=processor_transaction_id,
        )
        return CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=amount,
            status="completed",
            processor_transaction_id=processor_transaction_id,
            settled_at=timezone.now(),
            account_snapshot={"id": str(self.account.id)},
        )

    def test_failed_transaction_webhook_reverses_false_settlement(self):
        collection = self._settled_collection("0737cdbf-6c4c-4990-be61-c48b3fce78f7")
        payload = {
            "Type": "Transaction",
            "Data": {
                "Id": collection.processor_transaction_id,
                "TransactionStatus": "Failed",
                "FailedTransactionEvent": "EftFailedInsufficientFunds",
                "FailedAt": timezone.now().isoformat(),
                "TransactionHistory": [
                    {"Event": "Succeeded"},
                    {"Event": "EftFailedInsufficientFunds"},
                ],
            },
        }

        first = self.post_webhook(payload)
        duplicate = self.post_webhook(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        collection.refresh_from_db()
        collection.payment.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.zum_status, "Failed")
        self.assertEqual(collection.payment.status, "nsf")
        self.assertEqual(self.loan.balance, Decimal("697.18"))
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f"Collection failure id: {collection.id}",
            ).count(),
            1,
        )

    def test_completed_status_does_not_hide_failed_transaction_status(self):
        collection = self._settled_collection("68d39785-2067-43b5-b119-c7d42bc3abe2", "175.00")
        response = self.post_webhook({
            "Type": "Transaction",
            "Data": {
                "Id": collection.processor_transaction_id,
                "Status": "Completed",
                "TransactionStatus": "Failed",
                "FailedTransactionEvent": "EftFailedInsufficientFunds",
            },
        })

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.zum_status, "Failed")

    def test_duplicate_failed_webhook_heals_if_collection_still_completed(self):
        collection = self._settled_collection("306ff1db-3bb0-4cb9-8b1f-5eabd2c3aa5e", "176.61")
        payload = {
            "Type": "Transaction",
            "Data": {
                "Id": collection.processor_transaction_id,
                "TransactionStatus": "Failed",
                "FailedTransactionEvent": "EftFailedInsufficientFunds",
            },
        }
        raw, _signature = self.sign(payload)
        WebhookEvent.objects.create(
            payload_hash=payload_hash(raw),
            processor_transaction_id=collection.processor_transaction_id,
            webhook_type="Transaction",
            event_name="Failed",
            payload=payload,
            processed_at=timezone.now(),
        )

        response = self.post_webhook(payload)

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        collection.payment.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.payment.status, "nsf")

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_sync_from_zum_heals_completed_collection_when_history_has_nsf(self, mock_get_tx):
        mock_get_tx.return_value = {
            "Id": "a7b7193a-1831-4aaa-bbbb-cccccccccccc",
            "TransactionStatus": "Completed",
            "TransactionHistory": [
                {"Event": "Succeeded"},
                {"Event": "EftFailedInsufficientFunds"},
            ],
        }
        collection = self._settled_collection("a7b7193a-1831-4aaa-bbbb-cccccccccccc")

        synced = SettlementService.sync_from_zum(collection)

        self.assertEqual(synced.status, "failed")
        self.assertEqual(synced.failure_reason, "EftFailedInsufficientFunds")
        mock_get_tx.assert_called_once_with(collection.processor_transaction_id)

    @patch("loans.zumrails.ZumRailsService.get_transaction")
    def test_heal_missed_collection_failures_command_reverses_false_settlement(self, mock_get_tx):
        from django.core.management import call_command

        mock_get_tx.return_value = {
            "Id": "486482c4-d017-4aaa-bbbb-cccccccccccc",
            "TransactionStatus": "Failed",
            "FailedTransactionEvent": "EftFailedInsufficientFunds",
        }
        collection = self._settled_collection("486482c4-d017-4aaa-bbbb-cccccccccccc")

        call_command(
            "heal_missed_collection_failures",
            collection.processor_transaction_id,
        )

        collection.refresh_from_db()
        collection.payment.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.payment.status, "nsf")
        self.assertEqual(self.loan.balance, Decimal("697.18"))


@override_settings(
    ZUMRAILS_DRY_RUN=False,
    ZUMRAILS_API_BASE_URL="https://sandbox.example",
    ZUMRAILS_USERNAME="api-user",
    ZUMRAILS_PASSWORD="api-password",
    ZUMRAILS_WALLET_ID="wallet-1",
)
class ZumRailsClientTests(APITestCase):
    def setUp(self):
        cache.clear()

    @staticmethod
    def response(data, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = data
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(
                response=response
            )
        return response

    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_authenticates_and_sends_documented_eft_payload(
        self,
        mock_post,
        mock_request,
    ):
        mock_post.return_value = self.response({"result": {"Token": "token-1"}})
        mock_request.return_value = self.response({"result": {"Id": "transaction-1"}})
        client_id = uuid.uuid4()

        result = ZumRailsService.initiate_transaction(
            amount=Decimal("123.45"),
            transaction_type="AccountsPayable",
            method="Eft",
            memo="Loan 12345678",
            comment="Loan disbursement",
            client_transaction_id=client_id,
            user_payload={
                "FirstName": "Jane",
                "LastName": "Doe",
                "Email": "jane@example.com",
                "BankAccountInformation": {
                    "InstitutionNumber": "003",
                    "TransitNumber": "12345",
                    "AccountNumber": "1234567",
                },
            },
        )

        self.assertEqual(result, "transaction-1")
        mock_post.assert_called_once_with(
            "https://sandbox.example/api/authorize",
            json={"Username": "api-user", "Password": "api-password"},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        request_kwargs = mock_request.call_args.kwargs
        self.assertEqual(mock_request.call_args.args[:2], ("POST", "https://sandbox.example/api/transaction"))
        self.assertEqual(request_kwargs["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(request_kwargs["headers"]["idempotency-key"], str(client_id))
        self.assertEqual(request_kwargs["json"]["ZumRailsType"], "AccountsPayable")
        self.assertEqual(request_kwargs["json"]["TransactionMethod"], "Eft")
        self.assertEqual(request_kwargs["json"]["ClientTransactionId"], str(client_id))
        self.assertEqual(request_kwargs["json"]["WalletId"], "wallet-1")
        self.assertNotIn("Type", request_kwargs["json"])
        self.assertNotIn("Method", request_kwargs["json"])

    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_interac_payload_and_token_cache(self, mock_post, mock_request):
        mock_post.return_value = self.response({"Token": "token-1"})
        mock_request.side_effect = [
            self.response({"Id": "interac-1"}),
            self.response({"Id": "interac-2"}),
        ]

        for index in range(2):
            ZumRailsService.initiate_transaction(
                amount=Decimal("50.00"),
                transaction_type="AccountsPayable",
                method="Interac",
                memo=f"Loan {index}",
                client_transaction_id=uuid.uuid4(),
                user_payload={
                    "FirstName": "Jane",
                    "LastName": "Doe",
                    "Email": "jane@example.com",
                },
            )

        self.assertEqual(mock_post.call_count, 1)
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["InteracNotificationChannel"], "email")
        # Non-AutoDeposit emails require Q&A — must not omit security question.
        self.assertTrue(payload["InteracHasSecurityQuestionAndAnswer"])
        self.assertEqual(payload["InteracSecurityQuestion"], "Mohawk")
        self.assertEqual(payload["InteracSecurityAnswer"], "Loans")

    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_401_reauthenticates_once(self, mock_post, mock_request):
        mock_post.side_effect = [
            self.response({"Token": "expired-token"}),
            self.response({"Token": "fresh-token"}),
        ]
        mock_request.side_effect = [
            self.response({"error": "unauthorized"}, status_code=401),
            self.response({"Id": "transaction-1"}),
        ]

        result = ZumRailsService.initiate_transaction(
            amount=Decimal("10.00"),
            transaction_type="AccountsReceivable",
            method="Eft",
            memo="Loan 1",
            client_transaction_id=uuid.uuid4(),
            user_payload={"FirstName": "Jane", "LastName": "Doe", "Email": "jane@example.com"},
        )

        self.assertEqual(result, "transaction-1")
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            mock_request.call_args.kwargs["headers"]["Authorization"],
            "Bearer fresh-token",
        )

    @override_settings(ZUMRAILS_WALLET_ID="")
    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_discovers_and_uses_cad_wallet(self, mock_post, mock_request):
        mock_post.return_value = self.response({"Token": "token-1"})
        mock_request.side_effect = [
            self.response({
                "result": [
                    {"Id": "wallet-us", "Currency": "USD"},
                    {"Id": "wallet-ca", "Currency": "CAD"},
                ]
            }),
            self.response({"result": {"Id": "transaction-1"}}),
        ]

        ZumRailsService.initiate_transaction(
            amount=Decimal("10.00"),
            transaction_type="AccountsReceivable",
            method="Eft",
            memo="Loan 1",
            client_transaction_id=uuid.uuid4(),
            user_payload={"FirstName": "Jane", "LastName": "Doe", "Email": "jane@example.com"},
        )

        self.assertEqual(mock_request.call_args_list[0].args[:2], (
            "GET",
            "https://sandbox.example/api/wallet",
        ))
        self.assertEqual(
            mock_request.call_args_list[1].kwargs["json"]["WalletId"],
            "wallet-ca",
        )

    @override_settings(ZUMRAILS_WALLET_ID="", ZUMRAILS_FUNDING_SOURCE_ID="funding-source-1")
    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_uses_funding_source_when_wallet_unset(self, mock_post, mock_request):
        mock_post.return_value = self.response({"result": {"Token": "token-1"}})
        mock_request.return_value = self.response({"result": {"Id": "transaction-1"}})

        ZumRailsService.initiate_transaction(
            amount=Decimal("10.00"),
            transaction_type="AccountsReceivable",
            method="Eft",
            memo="Loan 1",
            client_transaction_id=uuid.uuid4(),
            user_payload={"FirstName": "Jane", "LastName": "Doe", "Email": "jane@example.com"},
        )

        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["FundingSourceId"], "funding-source-1")
        self.assertNotIn("WalletId", payload)

    @patch("loans.zumrails.requests.request")
    @patch("loans.zumrails.requests.post")
    def test_validate_and_process_ar_batch_payload(self, mock_post, mock_request):
        mock_post.return_value = self.response({"result": {"Token": "token-1"}})
        mock_request.side_effect = [
            self.response({"result": {"Status": "Ok", "InvalidTransactions": 0}}),
            self.response({"result": {"Id": "batch-1", "Status": "Ok"}}),
        ]
        csv_content = ZumRailsService.build_accounts_receivable_csv([
            {
                "first_name": "Jane",
                "last_name": "Example",
                "institution_number": "3",
                "transit_number": "45",
                "account_number": "1234567",
                "amount": "125.00",
                "comment": "Invoice 1001",
                "memo": "INV 1001",
            }
        ])
        self.assertIn("003;00045;1234567;12500;", csv_content.replace("\n", ""))

        result = ZumRailsService.process_accounts_receivable_batch(
            csv_content,
            idempotency_key=str(uuid.uuid4()),
            filename="batch_ar.csv",
        )

        self.assertEqual(result["result"]["Id"], "batch-1")
        self.assertEqual(
            mock_request.call_args_list[0].args[:2],
            ("POST", "https://sandbox.example/api/transaction/ValidateBatchFile"),
        )
        self.assertEqual(
            mock_request.call_args_list[1].args[:2],
            ("POST", "https://sandbox.example/api/transaction/ProcessBatchFile"),
        )
        process_payload = mock_request.call_args_list[1].kwargs["json"]
        self.assertEqual(process_payload["TransactionType"], "AccountsReceivable")
        self.assertEqual(process_payload["WalletId"], "wallet-1")
        self.assertTrue(process_payload["SkipFileAlreadyProcessedInLast24Hours"])
        self.assertEqual(process_payload["TransactionMethod"], "Eft")
        self.assertEqual(
            mock_request.call_args_list[0].kwargs["json"]["Bytes"],
            process_payload["Bytes"],
        )


@override_settings(ZUMRAILS_DRY_RUN=True)
class PaymentScheduleIntegrityTests(APITestCase):
    """Edge cases for schedule adjust / installment edit / Balance After.

    Covers the production failure mode where a Pending collection survived
    Adjust Schedule, producing same-day duplicates ($295 + $0.01) and multiple
    trailing Balance After $0 rows.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            email="schedule-integrity@example.com",
            password="password123",
            full_name="Schedule Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="schedule-customer@example.com",
            password="password123",
            full_name="Schedule Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Schedule",
            last_name="Customer",
            email="schedule-customer@example.com",
            phone="4165559999",
            phone_normalized="4165559999",
            province="ON",
            status="active",
            banking_verified=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.formula = LoanFormula.objects.create(
            name="Schedule Integrity 500",
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
            contract_signed_at=timezone.now(),
            funded_at=timezone.now(),
            formula=self.formula,
            is_active=True,
        )
        self.client.force_authenticate(self.staff)

    def _add_payment(self, amount, *, days=0, status="scheduled", notes=None):
        return Payment.objects.create(
            loan=self.loan,
            amount=Decimal(str(amount)),
            scheduled_date=timezone.localdate() + timedelta(days=days),
            status=status,
            notes=notes,
            processed_at=timezone.now() if status == "completed" else None,
        )

    def _open_sum(self):
        return sum(
            (
                p.amount
                for p in self.loan.payments.exclude(status__in=["completed", "cancelled"])
            ),
            Decimal("0.00"),
        )

    def _balance_after_map(self):
        from loans.serializers import CustomerLoanPaymentSerializer

        rows = list(self.loan.payments.exclude(status="cancelled"))
        data = CustomerLoanPaymentSerializer(rows, many=True).data
        return {row["id"]: Decimal(str(row["balance_after"])) for row in data}

    def test_stephanie_scenario_adjust_blocked_while_pending_keeps_single_row(self):
        """Pending $295 must not be layered with a new Adjust Schedule start row."""
        pending = self._add_payment("295.00", status="pending")
        self._add_payment("147.18", days=14)
        self._add_payment("147.18", days=28)
        before_ids = set(self.loan.payments.values_list("id", flat=True))

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "147.18",
                "frequency": "bi-weekly",
                "start_date": timezone.localdate().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("pending", response.data["error"].lower())
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.amount, Decimal("295.00"))
        self.assertEqual(set(self.loan.payments.values_list("id", flat=True)), before_ids)
        # No same-day scheduled twin created beside the pending row.
        same_day = self.loan.payments.filter(scheduled_date=pending.scheduled_date)
        self.assertEqual(same_day.count(), 1)

    def test_adjust_blocked_when_collection_attempt_processing_even_if_still_scheduled(self):
        payment = self._add_payment("147.18")
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=payment.amount,
            status="processing",
        )

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "weekly",
                "start_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())

    def test_adjust_clears_failed_and_nsf_then_rebuilds_without_duplicates(self):
        completed = self._add_payment("100.00", days=-14, status="completed")
        failed = self._add_payment("147.18", status="failed")
        nsf = self._add_payment("147.18", days=14, status="nsf")
        scheduled = self._add_payment("147.18", days=28)

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "bi-weekly",
                "start_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertTrue(Payment.objects.filter(pk=completed.pk, status="completed").exists())
        self.assertFalse(Payment.objects.filter(pk=failed.pk).exists())
        self.assertFalse(Payment.objects.filter(pk=nsf.pk).exists())
        self.assertFalse(Payment.objects.filter(pk=scheduled.pk).exists())
        rebuilt = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date", "created_at")
        )
        self.assertGreaterEqual(len(rebuilt), 1)
        dates = [p.scheduled_date for p in rebuilt]
        self.assertEqual(len(dates), len(set(dates)), "rebuilt schedule must not duplicate dates")
        rebuilt_total = sum((p.amount for p in rebuilt), Decimal("0.00"))
        self.assertEqual(rebuilt_total, self.loan.balance)
        self.assertEqual(
            rebuilt_total + completed.amount,
            self.loan.total_amount,
        )

    def test_adjust_succeeds_after_pending_clears_to_failed(self):
        pending = self._add_payment("295.00", status="pending")
        self._add_payment("147.18", days=14)

        blocked = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "bi-weekly",
                "start_date": timezone.localdate().isoformat(),
            },
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)

        pending.status = "failed"
        pending.failure_reason = "NSF"
        pending.save(update_fields=["status", "failure_reason"])

        ok = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "180.00",
                "frequency": "bi-weekly",
                "start_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertFalse(Payment.objects.filter(pk=pending.pk).exists())
        self.assertFalse(self.loan.payments.filter(status="pending").exists())

    def test_balloon_edit_removes_trailing_stub_and_single_terminal_zero(self):
        """Stephanie-style overshoot: balloon + tiny stub + full tail → rebalance."""
        first = self._add_payment("147.18")
        stub = self._add_payment("0.01")
        self._add_payment("147.18", days=14)
        self._add_payment("147.18", days=28)
        self._add_payment("147.18", days=42)
        self._add_payment("147.18", days=56)
        self._add_payment("147.19", days=70)
        # 147.18*5 + 0.01 + 147.19 = 883.10 → already 0.01 over total.
        self.assertGreater(self._open_sum(), self.loan.total_amount)

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "295.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("295.00"))
        self.assertEqual(self._open_sum(), self.loan.total_amount)

        balances = list(self._balance_after_map().values())
        # Consistent schedule → exactly one terminal Balance After $0.
        self.assertEqual(balances[-1], Decimal("0.00"))
        self.assertTrue(all(b > 0 for b in balances[:-1]), balances)
        self.assertEqual(balances.count(Decimal("0.00")), 1)

    def test_balloon_edit_does_not_reduce_pending_sibling(self):
        pending = self._add_payment("200.00", status="pending")
        first = self._add_payment("100.00", days=14)
        later = self._add_payment("583.09", days=28)

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "400.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(pending.amount, Decimal("200.00"))
        self.assertEqual(first.amount, Decimal("400.00"))
        # Remaining overshoot is taken from later scheduled rows only.
        if Payment.objects.filter(pk=later.pk).exists():
            later.refresh_from_db()
            self.assertLess(later.amount, Decimal("583.09"))
        self.assertEqual(self._open_sum(), self.loan.total_amount)

    def test_balloon_edit_preserves_deferral_fee_row(self):
        from loans.services import LoanService

        first = self._add_payment("100.00")
        fee = self._add_payment(
            "35.00",
            days=0,
            notes=LoanService.DEFERRAL_FEE_NOTE,
        )
        later = self._add_payment("748.09", days=14)

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "500.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Payment.objects.filter(pk=fee.pk, amount=Decimal("35.00")).exists())
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("500.00"))
        if Payment.objects.filter(pk=later.pk).exists():
            later.refresh_from_db()
            self.assertEqual(
                first.amount + fee.amount + later.amount,
                self.loan.total_amount,
            )

    def test_lowering_installment_does_not_auto_inflate_others(self):
        first = self._add_payment("300.00")
        second = self._add_payment("300.00", days=14)
        third = self._add_payment("283.09", days=28)

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(first.amount, Decimal("50.00"))
        self.assertEqual(second.amount, Decimal("300.00"))
        self.assertEqual(third.amount, Decimal("283.09"))
        self.assertLess(self._open_sum(), self.loan.total_amount)

    def test_date_only_edit_does_not_rebalance_amounts(self):
        first = self._add_payment("200.00")
        second = self._add_payment("683.09", days=14)
        new_date = timezone.localdate() + timedelta(days=3)

        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"scheduled_date": new_date.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.original_date, new_date)
        self.assertEqual(first.scheduled_date, previous_business_day(new_date))
        self.assertEqual(first.amount, Decimal("200.00"))
        self.assertEqual(second.amount, Decimal("683.09"))

    def test_rebalance_accounts_for_completed_payments(self):
        self._add_payment("200.00", days=-14, status="completed")
        self.loan.balance = Decimal("683.09")
        self.loan.save(update_fields=["balance", "updated_at"])
        first = self._add_payment("100.00")
        second = self._add_payment("300.00", days=14)
        third = self._add_payment("283.09", days=28)
        # Open sum currently 683.09; balloon first by 200 → overshoot 200.
        response = self.client.patch(
            f"/api/payments/{first.id}/",
            {"amount": "300.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("300.00"))
        open_total = self._open_sum()
        completed_total = sum(
            (p.amount for p in self.loan.payments.filter(status="completed")),
            Decimal("0.00"),
        )
        self.assertEqual(open_total + completed_total, self.loan.total_amount)

    def test_balance_after_skips_cancelled_and_matches_display_order(self):
        older = self._add_payment("300.00")
        cancelled = self._add_payment("50.00")
        cancelled.status = "cancelled"
        cancelled.save(update_fields=["status"])
        newer = self._add_payment("583.09")

        ordered = list(self.loan.payments.all())
        self.assertEqual([p.id for p in ordered], [older.id, cancelled.id, newer.id])

        balances = self._balance_after_map()
        self.assertNotIn(str(cancelled.id), balances)
        self.assertEqual(balances[str(older.id)], Decimal("583.09"))
        self.assertEqual(balances[str(newer.id)], Decimal("0.00"))

    def test_balance_after_same_day_pending_then_scheduled_is_monotonic(self):
        pending = self._add_payment("295.00", status="pending")
        scheduled = self._add_payment("0.01")
        # Force overshoot schedule like production screenshot, without edit API.
        self._add_payment("147.18", days=14)
        self._add_payment("147.18", days=28)
        self._add_payment("147.18", days=42)
        self._add_payment("147.18", days=56)
        self._add_payment("147.19", days=70)

        rows = list(self.loan.payments.exclude(status="cancelled"))
        self.assertEqual(rows[0].id, pending.id)
        self.assertEqual(rows[1].id, scheduled.id)

        balances = [self._balance_after_map()[str(p.id)] for p in rows]
        # Display order matches cumulative subtraction order (no newer-first inversion).
        self.assertEqual(balances[0], Decimal("588.09"))
        self.assertEqual(balances[1], Decimal("588.08"))
        for earlier, later in zip(balances, balances[1:]):
            self.assertGreaterEqual(earlier, later)
        # Overshoot still clamps, but values stay non-increasing in table order.
        self.assertEqual(balances[-1], Decimal("0.00"))

    def test_collection_failure_fee_does_not_make_failed_payment_reduce_balance(self):
        failed = self._add_payment("147.18", status="failed")
        second = self._add_payment("147.18", days=14)
        third = self._add_payment("147.18", days=28)
        fourth = self._add_payment("147.18", days=42)
        fifth = self._add_payment("147.18", days=56)
        last = self._add_payment("147.19", days=70)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedAccountClosed",
        )

        fee = LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedAccountClosed",
        )

        last.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(last.amount, Decimal("147.19"))
        self.assertEqual(fee.amount, Decimal("73.71"))
        recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE
        )
        self.assertEqual(
            recovery.scheduled_date,
            last.scheduled_date + timedelta(days=14),
        )
        self.assertEqual(fee.scheduled_date, last.scheduled_date + timedelta(days=28))
        self.assertEqual(self.loan.status, "stopped")
        self.assertEqual(recovery.amount, Decimal("147.18"))
        self.assertEqual(recovery.status, "unscheduled")
        self.assertFalse(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_INTEREST_NOTE,
            ).exists()
        )
        self.assertEqual(self.loan.balance, Decimal("956.80"))
        self.assertEqual(self.loan.total_amount, Decimal("956.80"))

        balances = self._balance_after_map()
        self.assertEqual(balances[str(failed.id)], Decimal("956.80"))
        self.assertEqual(balances[str(second.id)], Decimal("809.62"))
        self.assertEqual(balances[str(third.id)], Decimal("662.44"))
        self.assertEqual(balances[str(fourth.id)], Decimal("515.26"))
        self.assertEqual(balances[str(fifth.id)], Decimal("368.08"))
        self.assertEqual(balances[str(last.id)], Decimal("220.89"))
        self.assertEqual(balances[str(recovery.id)], Decimal("73.71"))
        self.assertEqual(balances[str(fee.id)], Decimal("0.00"))

        response = self.client.get(f"/api/loans/{self.loan.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        detail_balances = {
            row["id"]: Decimal(str(row["balance_after"]))
            for row in response.data["payments"]
        }
        self.assertEqual(detail_balances[str(failed.id)], Decimal("956.80"))
        self.assertEqual(detail_balances[str(last.id)], Decimal("220.89"))
        self.assertEqual(detail_balances[str(recovery.id)], Decimal("73.71"))
        self.assertEqual(detail_balances[str(fee.id)], Decimal("0.00"))

        customer_response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(customer_response.status_code, 200, customer_response.data)
        customer_loan = customer_response.data[0]
        self.assertEqual(Decimal(str(customer_loan["balance"])), Decimal("956.80"))
        self.assertEqual(Decimal(str(customer_loan["collectedAmount"])), Decimal("0.00"))
        customer_balances = {
            row["id"]: Decimal(str(row["balance_after"]))
            for row in customer_loan["paymentSchedule"]
        }
        self.assertEqual(customer_balances[str(failed.id)], Decimal("956.80"))
        self.assertEqual(customer_balances[str(fee.id)], Decimal("0.00"))

    def test_collection_failure_logic_reapplies_for_second_failed_payment(self):
        first = self._add_payment("147.18", status="failed")
        second = self._add_payment("147.18", days=14, status="failed")
        self._add_payment("147.18", days=28)
        self._add_payment("147.18", days=42)
        self._add_payment("147.18", days=56)
        original_last = self._add_payment("147.19", days=70)

        first_collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=first,
            amount=first.amount,
            status="failed",
            failure_reason="EftFailedAccountClosed",
        )
        LoanService.apply_collection_failure_fee(
            first_collection,
            reason="EftFailedAccountClosed",
        )

        self.loan.refresh_from_db()
        first_fee = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            notes__contains=f"Collection failure id: {first_collection.id}",
        )
        self.assertEqual(self.loan.balance, Decimal("956.80"))
        self.assertEqual(first_fee.amount, Decimal("73.71"))
        self.assertEqual(
            first_fee.scheduled_date,
            original_last.scheduled_date + timedelta(days=28),
        )

        second_collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=second,
            amount=second.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        LoanService.apply_collection_failure_fee(
            second_collection,
            reason="EftFailedInsufficientFunds",
        )

        self.loan.refresh_from_db()
        second_recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
            notes__contains=f"Collection failure id: {second_collection.id}",
        )
        second_fees = list(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).order_by("scheduled_date", "created_at", "id")
        )
        second_fee = second_fees[0]

        self.assertEqual(second_recovery.amount, Decimal("147.18"))
        self.assertEqual(second_fee.id, first_fee.id)
        self.assertEqual(second_fee.amount, Decimal("147.18"))
        self.assertEqual(second_fees[1].amount, Decimal("2.22"))
        self.assertEqual(
            second_recovery.scheduled_date,
            first_fee.scheduled_date - timedelta(days=14),
        )
        self.assertEqual(second_fee.scheduled_date, first_fee.scheduled_date)
        self.assertEqual(
            second_fees[1].scheduled_date,
            first_fee.scheduled_date + timedelta(days=14),
        )
        self.assertEqual(self.loan.balance, Decimal("1032.49"))
        self.assertEqual(self.loan.total_amount, Decimal("1032.49"))

        recovery_count = self.loan.payments.filter(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
        ).count()
        fee_count = self.loan.payments.filter(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        ).count()
        interest_count = self.loan.payments.filter(
            notes__startswith=LoanService.COLLECTION_FAILURE_INTEREST_NOTE,
        ).count()
        self.assertEqual(recovery_count, 2)
        self.assertEqual(fee_count, 2)
        self.assertEqual(interest_count, 0)

        balances = self._balance_after_map()
        self.assertEqual(balances[str(first.id)], Decimal("1032.49"))
        self.assertEqual(balances[str(second.id)], Decimal("1032.49"))
        self.assertEqual(balances[str(second_recovery.id)], Decimal("149.40"))
        self.assertEqual(balances[str(second_fee.id)], Decimal("2.22"))
        self.assertEqual(balances[str(second_fees[1].id)], Decimal("0.00"))

    def test_rebuild_collection_failure_schedule_converts_old_interest_rows(self):
        failed = self._add_payment("147.18", status="failed")
        second = self._add_payment("147.18", days=14)
        third = self._add_payment("147.18", days=28)
        fourth = self._add_payment("147.18", days=42)
        fifth = self._add_payment("147.18", days=56)
        last = self._add_payment("147.19", days=70)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedAccountClosed",
        )
        recovery = self._add_payment(
            "147.18",
            days=84,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_RECOVERY_NOTE} $147.18\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedAccountClosed"
            ),
        )
        fee = self._add_payment(
            "50.00",
            days=98,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedAccountClosed"
            ),
        )
        interest = self._add_payment(
            "23.71",
            days=112,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_INTEREST_NOTE}\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedAccountClosed\n"
                "Extension interest: $23.71"
            ),
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("956.80")
        self.loan.total_amount = Decimal("956.80")
        self.loan.fee = Decimal("456.80")
        self.loan.save(update_fields=["status", "is_active", "balance", "total_amount", "fee"])

        plan = LoanService.rebuild_collection_failure_schedule(
            self.loan,
            dry_run=False,
        )

        self.loan.refresh_from_db()
        self.assertEqual(plan["delete_count"], 3)
        self.assertEqual(plan["create_count"], 2)
        self.assertFalse(Payment.objects.filter(pk__in=[recovery.pk, fee.pk, interest.pk]).exists())
        new_recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
        )
        new_fee = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        )
        self.assertEqual(new_recovery.amount, Decimal("147.18"))
        self.assertEqual(new_recovery.scheduled_date, last.scheduled_date + timedelta(days=14))
        self.assertEqual(new_fee.amount, Decimal("73.71"))
        self.assertEqual(new_fee.scheduled_date, last.scheduled_date + timedelta(days=28))
        self.assertEqual(self.loan.balance, Decimal("956.80"))
        self.assertEqual(self.loan.total_amount, Decimal("956.80"))

        balances = self._balance_after_map()
        self.assertEqual(balances[str(failed.id)], Decimal("956.80"))
        self.assertEqual(balances[str(second.id)], Decimal("809.62"))
        self.assertEqual(balances[str(third.id)], Decimal("662.44"))
        self.assertEqual(balances[str(fourth.id)], Decimal("515.26"))
        self.assertEqual(balances[str(fifth.id)], Decimal("368.08"))
        self.assertEqual(balances[str(last.id)], Decimal("220.89"))
        self.assertEqual(balances[str(new_recovery.id)], Decimal("73.71"))
        self.assertEqual(balances[str(new_fee.id)], Decimal("0.00"))

    def test_rebuild_collection_failure_schedule_adds_missing_generated_rows(self):
        first = self._add_payment("176.61", status="completed")
        failed = self._add_payment("176.61", days=7, status="failed")
        third = self._add_payment("176.61", days=14)
        fourth = self._add_payment("176.61", days=21)
        last = self._add_payment("157.74", days=28)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedStopPayment",
        )
        self.formula.default_frequency_days = 7
        self.formula.save(update_fields=["default_frequency_days", "updated_at"])
        self.loan.balance = Decimal("687.57")
        self.loan.total_amount = Decimal("864.18")
        self.loan.fee = Decimal("364.18")
        self.loan.save(update_fields=["balance", "total_amount", "fee"])

        plan = LoanService.rebuild_collection_failure_schedule(
            self.loan,
            dry_run=False,
        )

        self.loan.refresh_from_db()
        recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
        )
        fee = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        )
        last.refresh_from_db()
        extra_interest = LoanService._period_interest_for_balance(
            self.loan,
            outstanding=Decimal("687.57"),
            days=14,
        )
        extra = LoanService.money(
            LoanService.COLLECTION_FAILURE_FEE_AMOUNT + extra_interest
        )
        leftover = extra - (Decimal("176.61") - Decimal("157.74"))
        self.assertEqual(leftover, Decimal("40.36"))
        self.assertEqual(plan["collections_count"], 1)
        self.assertEqual(plan["delete_count"], 0)
        self.assertEqual(plan["create_count"], 2)
        self.assertEqual(last.amount, Decimal("176.61"))
        self.assertEqual(recovery.amount, Decimal("176.61"))
        self.assertEqual(recovery.scheduled_date, last.scheduled_date + timedelta(days=7))
        self.assertEqual(fee.amount, leftover)
        self.assertEqual(fee.scheduled_date, last.scheduled_date + timedelta(days=14))
        self.assertIn(f"Collection failure id: {collection.id}", fee.notes)
        self.assertEqual(self.loan.balance, Decimal("746.80"))
        self.assertEqual(self.loan.total_amount, Decimal("923.41"))

        balances = self._balance_after_map()
        self.assertEqual(balances[str(first.id)], Decimal("746.80"))
        self.assertEqual(balances[str(failed.id)], Decimal("746.80"))
        self.assertEqual(balances[str(third.id)], Decimal("570.19"))
        self.assertEqual(balances[str(fourth.id)], Decimal("393.58"))
        self.assertEqual(balances[str(last.id)], Decimal("216.97"))
        self.assertEqual(balances[str(recovery.id)], leftover)
        self.assertEqual(balances[str(fee.id)], Decimal("0.00"))

    def test_collection_failure_fills_remainder_installment_up_to_cap(self):
        """NSF extra tops up the last original remainder before a leftover fee row."""
        first = self._add_payment("176.61", status="completed")
        failed = self._add_payment("176.61", days=7, status="failed")
        third = self._add_payment("176.61", days=14)
        fourth = self._add_payment("176.61", days=21)
        last = self._add_payment("157.74", days=28)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedStopPayment",
        )
        self.formula.default_frequency_days = 7
        self.formula.save(update_fields=["default_frequency_days", "updated_at"])
        self.loan.balance = Decimal("687.57")
        self.loan.total_amount = Decimal("864.18")
        self.loan.fee = Decimal("364.18")
        self.loan.save(update_fields=["balance", "total_amount", "fee"])
        extra_interest = LoanService._deferral_extra_interest(self.loan, 14)
        extra = LoanService.money(
            LoanService.COLLECTION_FAILURE_FEE_AMOUNT + extra_interest
        )
        leftover = extra - (Decimal("176.61") - Decimal("157.74"))
        self.assertEqual(leftover, Decimal("40.36"))

        fee = LoanService.apply_collection_failure_fee(
            collection,
            reason="EftFailedStopPayment",
        )

        last.refresh_from_db()
        self.loan.refresh_from_db()
        recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE
        )
        self.assertEqual(last.amount, Decimal("176.61"))
        self.assertEqual(recovery.amount, Decimal("176.61"))
        self.assertEqual(fee.amount, leftover)
        self.assertEqual(self.loan.balance, Decimal("687.57") + extra)
        self.assertEqual(
            [p.amount for p in self.loan.payments.exclude(status="cancelled").order_by(
                "scheduled_date", "created_at", "id"
            )],
            [
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                leftover,
            ],
        )
        balances = self._balance_after_map()
        self.assertEqual(balances[str(first.id)], self.loan.balance)
        self.assertEqual(balances[str(failed.id)], self.loan.balance)
        self.assertEqual(balances[str(third.id)], self.loan.balance - Decimal("176.61"))
        self.assertEqual(balances[str(fourth.id)], self.loan.balance - Decimal("353.22"))
        self.assertEqual(balances[str(last.id)], leftover + recovery.amount)
        self.assertEqual(balances[str(recovery.id)], leftover)
        self.assertEqual(balances[str(fee.id)], Decimal("0.00"))

    def test_collection_failure_nsf_fee_leftover_is_31_13_after_remainder_fill(self):
        """$157.74 remainder fills to $176.61; leftover $50 NSF is $31.13 on the last row."""
        first = self._add_payment("176.61", status="completed")
        failed = self._add_payment("176.61", days=7, status="failed")
        third = self._add_payment("176.61", days=14)
        fourth = self._add_payment("176.61", days=21)
        last = self._add_payment("157.74", days=28)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedStopPayment",
        )
        self.formula.default_frequency_days = 7
        self.formula.save(update_fields=["default_frequency_days", "updated_at"])
        self.loan.balance = Decimal("687.57")
        self.loan.total_amount = Decimal("864.18")
        self.loan.fee = Decimal("364.18")
        self.loan.save(update_fields=["balance", "total_amount", "fee"])
        leftover = Decimal("31.13")

        with patch.object(LoanService, "_deferral_extra_interest", return_value=Decimal("0.00")):
            fee = LoanService.apply_collection_failure_fee(
                collection,
                reason="EftFailedStopPayment",
            )

        last.refresh_from_db()
        self.loan.refresh_from_db()
        recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE
        )
        self.assertEqual(last.amount, Decimal("176.61"))
        self.assertEqual(recovery.amount, Decimal("176.61"))
        self.assertEqual(fee.amount, leftover)
        self.assertEqual(self.loan.balance, Decimal("737.57"))
        self.assertEqual(
            [p.amount for p in self.loan.payments.exclude(status="cancelled").order_by(
                "scheduled_date", "created_at", "id"
            )],
            [
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                Decimal("176.61"),
                leftover,
            ],
        )
        balances = self._balance_after_map()
        self.assertEqual(balances[str(first.id)], Decimal("737.57"))
        self.assertEqual(balances[str(failed.id)], Decimal("737.57"))
        self.assertEqual(balances[str(third.id)], Decimal("560.96"))
        self.assertEqual(balances[str(fourth.id)], Decimal("384.35"))
        self.assertEqual(balances[str(last.id)], Decimal("207.74"))
        self.assertEqual(balances[str(recovery.id)], leftover)
        self.assertEqual(balances[str(fee.id)], Decimal("0.00"))

    def test_rebuild_collection_failure_schedule_fills_existing_remainder_row(self):
        """Already-generated NSF rows still top up an under-cap original remainder."""
        self._add_payment("176.61", status="completed")
        failed = self._add_payment("176.61", days=7, status="failed")
        self._add_payment("176.61", days=14)
        self._add_payment("176.61", days=21)
        last = self._add_payment("157.74", days=28)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=failed,
            amount=failed.amount,
            status="failed",
            failure_reason="EftFailedStopPayment",
        )
        self.formula.default_frequency_days = 7
        self.formula.save(update_fields=["default_frequency_days", "updated_at"])
        extra_interest = Decimal("9.23")
        extra = Decimal("59.23")
        leftover = extra - (Decimal("176.61") - Decimal("157.74"))
        self.assertEqual(leftover, Decimal("40.36"))
        self._add_payment(
            "176.61",
            days=35,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_RECOVERY_NOTE} $176.61\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedStopPayment"
            ),
        )
        self._add_payment(
            "59.23",
            days=42,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedStopPayment\n"
                "NSF fee: $50.00\n"
                f"Extension interest: ${extra_interest}"
            ),
        )
        self.loan.balance = Decimal("746.80")
        self.loan.total_amount = Decimal("923.41")
        self.loan.fee = Decimal("423.41")
        self.loan.save(update_fields=["balance", "total_amount", "fee"])

        plan = LoanService.rebuild_collection_failure_schedule(
            self.loan,
            dry_run=False,
        )

        last.refresh_from_db()
        self.loan.refresh_from_db()
        recovery = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE
        )
        fee = self.loan.payments.get(
            notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE
        )
        self.assertEqual(plan["collections_count"], 1)
        self.assertEqual(last.amount, Decimal("176.61"))
        self.assertEqual(recovery.amount, Decimal("176.61"))
        self.assertEqual(fee.amount, leftover)
        self.assertEqual(self.loan.balance, Decimal("746.80"))
        self.assertEqual(self.loan.total_amount, Decimal("923.41"))

    def test_collections_nsf_rows_get_missing_fifty_dollar_fees(self):
        """In-collections NSF history without extras gets $50 NSF + remainder fill."""
        first = self._add_payment("175.25", status="nsf")
        second = self._add_payment("175.25", days=14, status="nsf")
        self._add_payment("175.25", days=28)
        self._add_payment("175.25", days=42)
        self._add_payment("175.25", days=56)
        remainder = self._add_payment("147.69", days=70)
        self._add_payment("175.25", days=84)
        self._add_payment("175.25", days=98)
        first.failure_reason = "Non-sufficient funds"
        first.save(update_fields=["failure_reason"])
        second.failure_reason = "Non-sufficient funds"
        second.save(update_fields=["failure_reason"])
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("1023.94")
        self.loan.total_amount = Decimal("1374.44")
        self.loan.fee = Decimal("874.44")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount", "fee"]
        )

        applied = LoanService.apply_missing_collection_failure_fees(
            self.loan,
            create_missing_collections=True,
        )

        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(len(applied), 2)
        self.assertEqual(remainder.amount, Decimal("175.25"))
        extras = list(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).order_by("scheduled_date", "created_at", "id")
        )
        self.assertGreaterEqual(len(extras), 1)
        self.assertTrue(
            all("NSF fee: $50.00" in (row.notes or "") for row in extras)
        )
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
            ).count(),
            2,
        )
        self.assertGreater(self.loan.balance, Decimal("1023.94"))
        balances = self._balance_after_map()
        last = (
            self.loan.payments.exclude(status="cancelled")
            .order_by("scheduled_date", "created_at", "id")
            .last()
        )
        self.assertEqual(balances[str(first.id)], self.loan.balance)
        self.assertEqual(balances[str(second.id)], self.loan.balance)
        self.assertEqual(balances[str(last.id)], Decimal("0.00"))

        again = LoanService.apply_missing_collection_failure_fees(
            self.loan,
            create_missing_collections=True,
        )
        self.assertEqual(again, [])
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).count(),
            len(extras),
        )

    def test_staff_nsf_endpoint_adds_collection_failure_fee(self):
        missed = self._add_payment("175.25")
        remainder = self._add_payment("147.69", days=14)
        self._add_payment("175.25", days=28)
        self.loan.balance = Decimal("498.19")
        self.loan.total_amount = Decimal("498.19")
        self.loan.save(update_fields=["balance", "total_amount"])

        response = self.client.post(f"/api/payments/{missed.id}/nsf/")
        self.assertEqual(response.status_code, 200, response.data)
        missed.refresh_from_db()
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(missed.status, "nsf")
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertTrue(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).exists()
        )
        self.assertGreater(self.loan.balance, Decimal("498.19"))

    def test_customer_loans_applies_missing_nsf_fees_for_failed_collections(self):
        missed = self._add_payment("175.25", status="nsf")
        remainder = self._add_payment("147.69", days=14)
        self._add_payment("175.25", days=28)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=missed,
            amount=missed.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("322.94")
        self.loan.total_amount = Decimal("498.19")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount"]
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(remainder.amount, Decimal("175.25"))
        payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        extra_rows = [
            row
            for row in payload["paymentSchedule"]
            if row.get("is_collection_failure_extra")
        ]
        self.assertGreaterEqual(len(extra_rows), 1)
        self.assertTrue(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f"Collection failure id: {collection.id}",
            ).exists()
        )

    def test_customer_loans_heals_nsf_rows_without_collection_records(self):
        """Marvin-style In Collections: NSF history, no extras, no CollectionPayment."""
        first = self._add_payment("175.25", status="nsf")
        second = self._add_payment("175.25", days=14, status="nsf")
        self._add_payment("175.25", days=28)
        remainder = self._add_payment("147.69", days=42)
        self._add_payment("175.25", days=56)
        first.failure_reason = "Non-sufficient funds"
        first.save(update_fields=["failure_reason"])
        second.failure_reason = "Non-sufficient funds"
        second.save(update_fields=["failure_reason"])
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("673.44")
        self.loan.total_amount = Decimal("1023.94")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount"]
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        extra_rows = [
            row
            for row in payload["paymentSchedule"]
            if row.get("is_collection_failure_extra")
        ]
        self.assertGreaterEqual(len(extra_rows), 1)
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertGreater(self.loan.balance, Decimal("673.44"))
        self.assertEqual(
            self.loan.payments.filter(status="nsf").count(),
            2,
        )

        second_response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(second_response.status_code, 200, second_response.data)
        second_payload = next(
            item for item in second_response.data if item["id"] == str(self.loan.id)
        )
        self.assertEqual(
            len([
                row
                for row in second_payload["paymentSchedule"]
                if row.get("is_collection_failure_extra")
            ]),
            len(extra_rows),
        )

    def test_customer_loans_does_not_reapply_existing_nsf_extra_row(self):
        """Vipan-style: leftover NSF extra already on the schedule stays untouched."""
        completed = self._add_payment("147.18", days=-14, status="completed")
        missed = self._add_payment("147.18", status="nsf")
        self._add_payment("147.18", days=14)
        extra_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        extra = self._add_payment(
            "66.37",
            days=28,
            notes=(
                f"{LoanService.COLLECTION_FAILURE_FEE_NOTE}\n"
                f"Collection failure id: {extra_id}\n"
                "NSF fee: $50.00\n"
                "Extension interest: $16.37"
            ),
        )
        CollectionPayment.objects.create(
            id=extra_id,
            loan=self.loan,
            payment=missed,
            amount=missed.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("802.28")
        self.loan.total_amount = Decimal("949.46")
        self.loan.fee = Decimal("449.46")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount", "fee"]
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        extra.refresh_from_db()
        missed.refresh_from_db()
        completed.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(extra.amount, Decimal("66.37"))
        self.assertEqual(missed.status, "nsf")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(self.loan.balance, Decimal("802.28"))
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).count(),
            1,
        )
        payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        extra_rows = [
            row
            for row in payload["paymentSchedule"]
            if row.get("is_collection_failure_extra")
        ]
        self.assertEqual(len(extra_rows), 1)
        self.assertEqual(Decimal(str(extra_rows[0]["amount"])), Decimal("66.37"))

    def test_customer_loans_heals_arrive_nsf_like_landing(self):
        """Arrive customer GET applies the same leftover NSF extra as Landing."""
        self.customer.source = Customer.SOURCE_ARRIVE
        self.customer.arrive_application_id = "arrive-schedule-nsf"
        self.customer.save(
            update_fields=["source", "arrive_application_id", "updated_at"]
        )
        first = self._add_payment("175.25", status="nsf")
        second = self._add_payment("175.25", days=14, status="nsf")
        self._add_payment("175.25", days=28)
        remainder = self._add_payment("147.69", days=42)
        first.failure_reason = "EftFailedInsufficientFunds"
        first.save(update_fields=["failure_reason"])
        second.failure_reason = "EftFailedInsufficientFunds"
        second.save(update_fields=["failure_reason"])
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=first,
            amount=first.amount,
            status="returned",
            failure_reason="EftFailedInsufficientFunds",
        )
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=second,
            amount=second.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("673.44")
        self.loan.total_amount = Decimal("1023.94")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount"]
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        extra_rows = [
            row
            for row in payload["paymentSchedule"]
            if row.get("is_collection_failure_extra")
        ]
        self.assertGreaterEqual(len(extra_rows), 1)
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertGreater(self.loan.balance, Decimal("673.44"))
        self.assertTrue(
            any(
                "NSF fee: $50.00" in (row.get("notes") or "")
                for row in extra_rows
            )
        )

        second_response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(second_response.status_code, 200, second_response.data)
        second_payload = next(
            item for item in second_response.data if item["id"] == str(self.loan.id)
        )
        self.assertEqual(
            len([
                row
                for row in second_payload["paymentSchedule"]
                if row.get("is_collection_failure_extra")
            ]),
            len(extra_rows),
        )

    def test_customer_loans_heals_marvin_arrive_nsf_with_completed_collections(self):
        """Marvin Arrive: two NSF rows, completed collections, remainder still $147.69."""
        self.customer.source = Customer.SOURCE_ARRIVE
        self.customer.arrive_application_id = "arrive-marvin-completed"
        self.customer.save(
            update_fields=["source", "arrive_application_id", "updated_at"]
        )
        first = self._add_payment("175.25", status="nsf")
        second = self._add_payment("175.25", days=14, status="nsf")
        self._add_payment("175.25", days=28)
        self._add_payment("175.25", days=42)
        self._add_payment("175.25", days=56)
        remainder = self._add_payment("147.69", days=70)
        self._add_payment("175.25", days=84)
        self._add_payment("175.25", days=98)
        first.failure_reason = "Non-sufficient funds"
        first.save(update_fields=["failure_reason"])
        second.failure_reason = "Non-sufficient funds"
        second.save(update_fields=["failure_reason"])
        first_collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=first,
            amount=first.amount,
            status="completed",
            failure_reason="Non-sufficient funds",
            settled_at=timezone.now() - timedelta(days=6),
        )
        second_collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=second,
            amount=second.amount,
            status="completed",
            failure_reason="Non-sufficient funds",
            settled_at=timezone.now() - timedelta(days=4),
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("1023.94")
        self.loan.total_amount = Decimal("1023.94")
        self.loan.save(
            update_fields=["status", "is_active", "balance", "total_amount"]
        )

        with patch.object(
            LoanService, "_deferral_extra_interest", return_value=Decimal("0.00")
        ):
            response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_collection.refresh_from_db()
        second_collection.refresh_from_db()
        self.loan.refresh_from_db()
        payload = next(
            item for item in response.data if item["id"] == str(self.loan.id)
        )
        extra_rows = [
            row
            for row in payload["paymentSchedule"]
            if row.get("is_collection_failure_extra")
        ]
        self.assertGreaterEqual(len(extra_rows), 1)
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertEqual(first.status, "nsf")
        self.assertEqual(second.status, "nsf")
        self.assertEqual(first_collection.status, "completed")
        self.assertEqual(second_collection.status, "completed")
        self.assertGreater(self.loan.balance, Decimal("1023.94"))
        self.assertTrue(
            any(
                "NSF fee: $50.00" in (row.get("notes") or "")
                for row in extra_rows
            )
        )

    def test_customer_loans_adds_nsf_extra_when_only_recovery_exists(self):
        """Recovery notes must not block the leftover NSF extra row."""
        missed = self._add_payment("175.25", status="nsf")
        remainder = self._add_payment("147.69", days=14)
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=missed,
            amount=missed.amount,
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("175.25"),
            scheduled_date=timezone.localdate() + timedelta(days=28),
            status="scheduled",
            notes=(
                f"{LoanService.COLLECTION_FAILURE_RECOVERY_NOTE} $175.25\n"
                f"Collection failure id: {collection.id}\n"
                "Reason: EftFailedInsufficientFunds"
            ),
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("322.94")
        self.loan.save(update_fields=["status", "is_active", "balance"])

        with patch.object(
            LoanService, "_deferral_extra_interest", return_value=Decimal("0.00")
        ):
            response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertEqual(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_RECOVERY_NOTE,
            ).count(),
            1,
        )
        self.assertTrue(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
                notes__contains=f"Collection failure id: {collection.id}",
            ).exists()
        )

    def test_customer_loans_heals_nsf_when_collection_still_processing(self):
        missed = self._add_payment("175.25", status="nsf")
        remainder = self._add_payment("147.69", days=14)
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=missed,
            amount=missed.amount,
            status="processing",
            failure_reason="EftFailedInsufficientFunds",
            zum_status="Failed",
        )
        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("322.94")
        self.loan.save(update_fields=["status", "is_active", "balance"])

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        remainder.refresh_from_db()
        self.assertEqual(remainder.amount, Decimal("175.25"))
        self.assertTrue(
            self.loan.payments.filter(
                notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE,
            ).exists()
        )

    def test_edit_rejects_payment_with_processing_collection(self):
        payment = self._add_payment("147.18")
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=payment,
            amount=payment.amount,
            status="processing",
        )

        response = self.client.patch(
            f"/api/payments/{payment.id}/",
            {"amount": "200.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("147.18"))

    def test_failed_installment_cannot_be_edited(self):
        failed = self._add_payment("100.00", status="failed")
        later = self._add_payment("783.09", days=14)

        response = self.client.patch(
            f"/api/payments/{failed.id}/",
            {"amount": "400.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("missed or failed", response.data["error"].lower())
        failed.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.amount, Decimal("100.00"))
        self.assertEqual(later.amount, Decimal("783.09"))

    def test_protects_edited_amount_when_no_other_scheduled_to_trim(self):
        """If only the edited row is scheduled, overshoot cannot be trimmed away."""
        only = self._add_payment("100.00")
        self._add_payment("200.00", status="pending")

        response = self.client.patch(
            f"/api/payments/{only.id}/",
            {"amount": "800.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        only.refresh_from_db()
        self.assertEqual(only.amount, Decimal("800.00"))
        # Pending sibling untouched; open sum may still exceed total.
        self.assertEqual(self.loan.payments.get(status="pending").amount, Decimal("200.00"))
        self.assertGreater(self._open_sum(), self.loan.total_amount)

    def test_adjust_schedule_can_run_multiple_times_when_no_pending_collection(self):
        """Staff may rebuild the schedule repeatedly before collections start."""
        self._add_payment("200.00")
        self._add_payment("683.09", days=14)
        start_a = timezone.localdate() + timedelta(days=3)
        start_b = timezone.localdate() + timedelta(days=10)

        first = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "payment_amount": "200.00",
                "frequency": "bi-weekly",
                "start_date": start_a.isoformat(),
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)
        after_first = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertGreaterEqual(len(after_first), 1)
        self.assertEqual(
            after_first[0].scheduled_date,
            previous_business_day(start_a),
        )

        second = self.client.patch(
            f"/api/loans/{self.loan.id}/adjust-schedule/",
            {
                "calculation_mode": "number_of_payments",
                "number_of_payments": 4,
                "frequency": "weekly",
                "start_date": start_b.isoformat(),
            },
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.data)
        self.loan.refresh_from_db()
        after_second = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertEqual(len(after_second), 4)
        self.assertEqual(
            after_second[0].scheduled_date,
            previous_business_day(start_b),
        )
        self.assertEqual(
            sum((p.amount for p in after_second), Decimal("0.00")),
            self.loan.balance,
        )
        # Prior rebuild rows are fully replaced.
        self.assertFalse(
            any(p.scheduled_date == start_a for p in after_second)
        )

    def test_record_interac_payment_reduces_balance_and_shortens_schedule(self):
        nsf = self._add_payment("147.18", status="nsf")
        second = self._add_payment("147.18", days=14)
        last = self._add_payment("147.18", days=28)
        self.loan.balance = Decimal("294.36")
        self.loan.save(update_fields=["balance", "updated_at"])
        received = timezone.localdate()

        response = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "100.00",
                "type": "etransfer",
                "received_date": received.isoformat(),
                "notes": "Interac received",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        nsf.refresh_from_db()
        interac = self.loan.payments.get(type="etransfer", status="completed")
        scheduled = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertEqual(interac.amount, Decimal("100.00"))
        self.assertEqual(interac.scheduled_date, received)
        self.assertEqual(interac.notes, "Interac received")
        self.assertEqual(self.loan.balance, Decimal("194.36"))
        self.assertEqual(nsf.status, "nsf")
        self.assertEqual(len(scheduled), 2)
        self.assertEqual(scheduled[0].id, second.id)
        self.assertEqual(scheduled[0].amount, Decimal("147.18"))
        self.assertEqual(scheduled[1].id, last.id)
        self.assertEqual(scheduled[1].amount, Decimal("47.18"))
        self.assertEqual(
            sum((p.amount for p in scheduled), Decimal("0.00")),
            self.loan.balance,
        )
        dates = [
            p.scheduled_date
            for p in self.loan.payments.exclude(status="cancelled").order_by(
                "scheduled_date", "created_at", "id"
            )
        ]
        self.assertEqual(dates, [nsf.scheduled_date, received, second.scheduled_date, last.scheduled_date])
        balances = self._balance_after_map()
        self.assertEqual(balances[str(nsf.id)], Decimal("294.36"))
        self.assertEqual(balances[str(interac.id)], Decimal("194.36"))
        self.assertEqual(balances[str(scheduled[0].id)], Decimal("47.18"))
        self.assertEqual(balances[str(scheduled[1].id)], Decimal("0.00"))

    def test_record_payment_received_date_must_be_today_or_up_to_three_days_ago(self):
        self._add_payment("147.18")
        self.loan.balance = Decimal("147.18")
        self.loan.save(update_fields=["balance", "updated_at"])
        today = timezone.localdate()

        future = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "10.00",
                "type": "etransfer",
                "received_date": (today + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(future.status_code, 400, future.data)

        too_old = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "10.00",
                "type": "etransfer",
                "received_date": (today - timedelta(days=4)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(too_old.status_code, 400, too_old.data)

        allowed = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "10.00",
                "type": "etransfer",
                "received_date": (today - timedelta(days=3)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)
        interac = self.loan.payments.get(type="etransfer", status="completed")
        self.assertEqual(interac.scheduled_date, today - timedelta(days=3))

    def test_normalize_received_payment_date_window(self):
        from loans.services import LoanService

        today = timezone.localdate()
        self.assertEqual(LoanService.normalize_received_payment_date(None), today)
        self.assertEqual(LoanService.normalize_received_payment_date(today), today)
        self.assertEqual(
            LoanService.normalize_received_payment_date(today - timedelta(days=3)),
            today - timedelta(days=3),
        )
        with self.assertRaises(ValueError):
            LoanService.normalize_received_payment_date(today + timedelta(days=1))
        with self.assertRaises(ValueError):
            LoanService.normalize_received_payment_date(today - timedelta(days=4))

    def test_apply_nsf_rebate_reduces_balance_and_shortens_schedule(self):
        nsf = self._add_payment("147.18", status="nsf")
        second = self._add_payment("147.18", days=14)
        last = self._add_payment("147.18", days=28)
        self.loan.status = "defaulted"
        self.loan.balance = Decimal("294.36")
        self.loan.save(update_fields=["status", "balance", "updated_at"])
        applied = timezone.localdate() + timedelta(days=3)

        response = self.client.post(
            f"/api/loans/{self.loan.id}/apply_rebate/",
            {
                "amount": "50.00",
                "reason": "nsf_discount",
                "applied_date": applied.isoformat(),
                "notes": "Waive NSF fee",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        nsf.refresh_from_db()
        rebate = self.loan.payments.get(type="rebate", status="completed")
        scheduled = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertEqual(rebate.amount, Decimal("50.00"))
        self.assertEqual(rebate.scheduled_date, applied)
        self.assertIn("Rebate: NSF discount", rebate.notes)
        self.assertIn("Waive NSF fee", rebate.notes)
        self.assertEqual(self.loan.balance, Decimal("244.36"))
        self.assertEqual(nsf.status, "nsf")
        self.assertEqual(nsf.amount, Decimal("147.18"))
        self.assertEqual(len(scheduled), 2)
        self.assertEqual(scheduled[0].id, second.id)
        self.assertEqual(scheduled[0].amount, Decimal("147.18"))
        self.assertEqual(scheduled[1].id, last.id)
        self.assertEqual(scheduled[1].amount, Decimal("97.18"))
        self.assertEqual(
            sum((p.amount for p in scheduled), Decimal("0.00")),
            self.loan.balance,
        )
        balances = self._balance_after_map()
        self.assertEqual(balances[str(nsf.id)], Decimal("294.36"))
        self.assertEqual(balances[str(rebate.id)], Decimal("244.36"))
        self.assertEqual(balances[str(scheduled[0].id)], Decimal("97.18"))
        self.assertEqual(balances[str(scheduled[1].id)], Decimal("0.00"))

        revert = self.client.post(
            f"/api/payments/{rebate.id}/revert-recorded/",
            {},
            format="json",
        )
        self.assertEqual(revert.status_code, 200, revert.data)
        self.loan.refresh_from_db()
        rebate.refresh_from_db()
        self.assertEqual(rebate.status, "cancelled")
        self.assertEqual(self.loan.balance, Decimal("294.36"))
        restored = list(
            self.loan.payments.filter(status="scheduled").order_by("scheduled_date")
        )
        self.assertEqual(
            sum((p.amount for p in restored), Decimal("0.00")),
            self.loan.balance,
        )

    def test_apply_rebate_rejects_overpay_pending_and_non_collecting_loans(self):
        self._add_payment("147.18")
        self.loan.balance = Decimal("147.18")
        self.loan.save(update_fields=["balance", "updated_at"])

        overpay = self.client.post(
            f"/api/loans/{self.loan.id}/apply_rebate/",
            {"amount": "200.00", "reason": "nsf_discount"},
            format="json",
        )
        self.assertEqual(overpay.status_code, 400, overpay.data)

        pending = self._add_payment("50.00", days=14, status="pending")
        self.loan.balance = Decimal("197.18")
        self.loan.save(update_fields=["balance", "updated_at"])
        in_flight = self.client.post(
            f"/api/loans/{self.loan.id}/apply_rebate/",
            {"amount": "10.00", "reason": "nsf_discount"},
            format="json",
        )
        self.assertEqual(in_flight.status_code, 400, in_flight.data)
        pending.delete()

        self.loan.status = "stopped"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "updated_at"])
        stopped = self.client.post(
            f"/api/loans/{self.loan.id}/apply_rebate/",
            {"amount": "10.00", "reason": "nsf_discount"},
            format="json",
        )
        self.assertEqual(stopped.status_code, 400, stopped.data)
        self.assertFalse(self.loan.payments.filter(type="rebate").exists())

    def test_record_payment_rejects_overpay_and_pending_loans(self):
        self._add_payment("147.18")
        self.loan.balance = Decimal("147.18")
        self.loan.save(update_fields=["balance", "updated_at"])

        overpay = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {"amount": "200.00", "type": "etransfer"},
            format="json",
        )
        self.assertEqual(overpay.status_code, 400, overpay.data)

        self.loan.status = "pending"
        self.loan.save(update_fields=["status", "updated_at"])
        pending_loan = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {"amount": "10.00", "type": "etransfer"},
            format="json",
        )
        self.assertEqual(pending_loan.status_code, 400, pending_loan.data)
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])

        pending = self._add_payment("50.00", days=14, status="pending")
        self.loan.balance = Decimal("197.18")
        self.loan.save(update_fields=["balance", "updated_at"])
        in_flight = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {"amount": "10.00", "type": "etransfer"},
            format="json",
        )
        self.assertEqual(in_flight.status_code, 200, in_flight.data)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.amount, Decimal("50.00"))
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.balance, Decimal("187.18"))
        pending.delete()

        self.loan.status = "defaulted"
        self.loan.is_active = False
        self.loan.balance = Decimal("147.18")
        self.loan.save(update_fields=["status", "is_active", "balance", "updated_at"])
        allowed = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {"amount": "47.18", "type": "etransfer", "notes": "Interac received"},
            format="json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.balance, Decimal("100.00"))
        self.assertTrue(
            self.loan.payments.filter(type="etransfer", status="completed").exists()
        )

    def test_record_interac_on_stopped_loan_credits_balance_without_restarting_pad(self):
        failed = self._add_payment("131.05", status="failed")
        failed.failure_reason = "EftFailedAccountClosed"
        failed.save(update_fields=["failure_reason"])
        later = self._add_payment("131.05", days=14)
        last = self._add_payment("61.64", days=28)
        self.loan.balance = Decimal("192.69")
        self.loan.status = "stopped"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "balance", "updated_at"])
        self.loan.unschedule_remaining_payments()
        later.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual(later.status, "unscheduled")
        self.assertEqual(last.status, "unscheduled")

        response = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "100.00",
                "type": "etransfer",
                "notes": "Interac received",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        later.refresh_from_db()
        interac = self.loan.payments.get(type="etransfer", status="completed")
        self.assertEqual(self.loan.status, "stopped")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(self.loan.balance, Decimal("92.69"))
        self.assertEqual(interac.amount, Decimal("100.00"))
        self.assertFalse(self.loan.payments.filter(status="scheduled").exists())
        remaining = list(
            self.loan.payments.filter(status="unscheduled").order_by("scheduled_date", "id")
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, later.id)
        self.assertEqual(remaining[0].amount, Decimal("92.69"))
        self.assertFalse(Payment.objects.filter(id=last.id).exists())

        revert = self.client.post(
            f"/api/payments/{interac.id}/revert-recorded/",
            {},
            format="json",
        )
        self.assertEqual(revert.status_code, 200, revert.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(self.loan.balance, Decimal("192.69"))
        self.assertFalse(self.loan.payments.filter(status="scheduled").exists())
        self.assertEqual(
            sum(
                (p.amount for p in self.loan.payments.filter(status="unscheduled")),
                Decimal("0.00"),
            ),
            Decimal("192.69"),
        )
        self.assertTrue(Payment.objects.filter(pk=failed.pk, status="failed").exists())

    def test_record_interac_payoff_on_stopped_loan_marks_paid_off(self):
        self._add_payment("131.05", status="failed")
        leftover = self._add_payment("92.69", days=14)
        self.loan.balance = Decimal("92.69")
        self.loan.status = "stopped"
        self.loan.is_active = False
        self.loan.save(update_fields=["status", "is_active", "balance", "updated_at"])
        self.loan.unschedule_remaining_payments()

        response = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "92.69",
                "type": "etransfer",
                "notes": "Interac received",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "paid_off")
        self.assertEqual(self.loan.balance, Decimal("0.00"))
        self.assertFalse(
            self.loan.payments.filter(status__in=["scheduled", "unscheduled"]).exists()
        )
        self.assertFalse(Payment.objects.filter(id=leftover.id).exists())

        interac = self.loan.payments.get(type="etransfer", status="completed")
        revert = self.client.post(
            f"/api/payments/{interac.id}/revert-recorded/",
            {},
            format="json",
        )
        self.assertEqual(revert.status_code, 200, revert.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "stopped")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(self.loan.balance, Decimal("92.69"))
        self.assertFalse(self.loan.payments.filter(status="scheduled").exists())
        self.assertEqual(
            sum(
                (p.amount for p in self.loan.payments.filter(status="unscheduled")),
                Decimal("0.00"),
            ),
            Decimal("92.69"),
        )

    def test_record_payment_credits_settled_balance_while_collection_processing(self):
        nsf = self._add_payment("147.18", status="nsf")
        pending = self._add_payment("147.18", days=14, status="pending")
        later = self._add_payment("147.18", days=28)
        self.loan.balance = Decimal("294.36")
        self.loan.save(update_fields=["balance", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            payment=pending,
            amount=Decimal("147.18"),
            status="processing",
            processor_transaction_id="in-flight-pad-1",
            settlement_due_at=timezone.now() - timedelta(minutes=1),
            account_snapshot={},
        )

        response = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "100.00",
                "type": "etransfer",
                "notes": "Interac received",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        collection.refresh_from_db()
        nsf.refresh_from_db()
        later.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertEqual(collection.status, "processing")
        self.assertEqual(nsf.status, "nsf")
        self.assertEqual(self.loan.balance, Decimal("194.36"))
        self.assertEqual(later.amount, Decimal("194.36"))

        SettlementService.complete_if_eligible(collection)
        collection.refresh_from_db()
        pending.refresh_from_db()
        later.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(collection.status, "completed")
        self.assertEqual(pending.status, "completed")
        self.assertEqual(self.loan.balance, Decimal("47.18"))
        self.assertEqual(later.amount, Decimal("47.18"))

    def test_nsf_and_past_due_installments_cannot_be_updated(self):
        nsf = self._add_payment("147.18", status="nsf")
        past_due = self._add_payment("147.18", days=-7)
        future = self._add_payment("147.18", days=14)

        nsf_response = self.client.patch(
            f"/api/payments/{nsf.id}/",
            {"amount": "100.00"},
            format="json",
        )
        self.assertEqual(nsf_response.status_code, 400, nsf_response.data)
        nsf.refresh_from_db()
        self.assertEqual(nsf.amount, Decimal("147.18"))
        self.assertEqual(nsf.status, "nsf")

        past_response = self.client.patch(
            f"/api/payments/{past_due.id}/",
            {"amount": "100.00"},
            format="json",
        )
        self.assertEqual(past_response.status_code, 400, past_response.data)
        past_due.refresh_from_db()
        self.assertEqual(past_due.amount, Decimal("147.18"))
        self.assertEqual(past_due.status, "scheduled")

        defer_nsf = self.client.post(f"/api/payments/{nsf.id}/defer/", {}, format="json")
        self.assertEqual(defer_nsf.status_code, 400, defer_nsf.data)

        future_response = self.client.patch(
            f"/api/payments/{future.id}/",
            {"amount": "160.00"},
            format="json",
        )
        self.assertEqual(future_response.status_code, 200, future_response.data)
        future.refresh_from_db()
        self.assertEqual(future.amount, Decimal("160.00"))

    def test_revert_and_update_recorded_interac_restores_schedule(self):
        nsf = self._add_payment("147.18", status="nsf")
        second = self._add_payment("147.18", days=14)
        last = self._add_payment("147.18", days=28)
        self.loan.balance = Decimal("294.36")
        self.loan.save(update_fields=["balance", "updated_at"])
        received = timezone.localdate()

        recorded = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": "100.00",
                "type": "etransfer",
                "received_date": received.isoformat(),
            },
            format="json",
        )
        self.assertEqual(recorded.status_code, 200, recorded.data)
        interac = self.loan.payments.get(type="etransfer", status="completed")

        updated = self.client.patch(
            f"/api/payments/{interac.id}/update-recorded/",
            {"amount": "50.00"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.loan.refresh_from_db()
        interac.refresh_from_db()
        last.refresh_from_db()
        nsf.refresh_from_db()
        self.assertEqual(interac.amount, Decimal("50.00"))
        self.assertEqual(self.loan.balance, Decimal("244.36"))
        self.assertEqual(nsf.amount, Decimal("147.18"))
        self.assertEqual(nsf.status, "nsf")
        self.assertEqual(second.amount, Decimal("147.18"))
        self.assertEqual(last.amount, Decimal("97.18"))

        rejected_nsf = self.client.patch(
            f"/api/payments/{nsf.id}/update-recorded/",
            {"amount": "10.00"},
            format="json",
        )
        self.assertEqual(rejected_nsf.status_code, 400, rejected_nsf.data)

        reverted = self.client.post(
            f"/api/payments/{interac.id}/revert-recorded/",
            {},
            format="json",
        )
        self.assertEqual(reverted.status_code, 200, reverted.data)
        self.loan.refresh_from_db()
        interac.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual(interac.status, "cancelled")
        self.assertEqual(self.loan.balance, Decimal("294.36"))
        self.assertEqual(self.loan.status, "active")
        self.assertEqual(last.amount, Decimal("147.18"))
        self.assertEqual(
            sum(
                (p.amount for p in self.loan.payments.filter(status="scheduled")),
                Decimal("0.00"),
            ),
            self.loan.balance,
        )

    def test_payoff_today_removes_unused_interest_and_marks_paid_off(self):
        self._add_payment(str(self.loan.balance), days=14)
        breakdown = self.client.get(f"/api/loans/{self.loan.id}/interest-breakdown/")
        self.assertEqual(breakdown.status_code, 200, breakdown.data)
        payoff = Decimal(str(breakdown.data["payoff_today"]))
        unused = Decimal(str(breakdown.data["unused_daily_interest"]))
        original_fee = self.loan.fee
        self.assertGreater(unused, Decimal("0.00"))
        self.assertLess(payoff, self.loan.balance)

        response = self.client.post(
            f"/api/loans/{self.loan.id}/record_payment/",
            {
                "amount": str(payoff),
                "type": "etransfer",
                "received_date": timezone.localdate().isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "paid_off")
        self.assertEqual(self.loan.balance, Decimal("0.00"))
        self.assertEqual(self.loan.fee, original_fee - unused)
        self.assertFalse(self.loan.payments.filter(status="scheduled").exists())
        interac = self.loan.payments.get(type="etransfer", status="completed")
        self.assertEqual(interac.amount, payoff)
        self.assertIn("Unused daily interest forgiven", interac.notes or "")

    def test_heal_recorded_payment_schedules_aligns_scheduled_to_balance(self):
        from io import StringIO

        from django.core.management import call_command

        self._add_payment("147.18", days=14)
        last = self._add_payment("147.18", days=28)
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            type="etransfer",
            status="completed",
            scheduled_date=timezone.localdate(),
            processed_at=timezone.now(),
        )
        self.loan.balance = Decimal("244.36")
        self.loan.save(update_fields=["balance", "updated_at"])

        out = StringIO()
        call_command("heal_recorded_payment_schedules", "--apply", stdout=out)
        last.refresh_from_db()
        self.assertEqual(last.amount, Decimal("97.18"))
        self.assertEqual(
            sum(
                (p.amount for p in self.loan.payments.filter(status="scheduled")),
                Decimal("0.00"),
            ),
            self.loan.balance,
        )
        self.assertIn("Updated 1", out.getvalue())


@override_settings(ZUMRAILS_DRY_RUN=True)
class HealScheduleKeepingPendingTests(APITestCase):
    """Ops heal script: keep Pending, rebuild upcoming, dry-run before apply."""

    def setUp(self):
        from loans.services import LoanService

        self.LoanService = LoanService
        self.staff = User.objects.create_user(
            email="heal-schedule@example.com",
            password="password123",
            full_name="Heal Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="heal-customer@example.com",
            password="password123",
            full_name="Heal Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Heal",
            last_name="Customer",
            email="heal-customer@example.com",
            phone="4165558888",
            phone_normalized="4165558888",
            province="ON",
            status="active",
            banking_verified=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.formula = LoanFormula.objects.create(
            name="Heal Schedule 500",
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
            contract_signed_at=timezone.now(),
            funded_at=timezone.now(),
            formula=self.formula,
            is_active=True,
        )

    def _add(self, amount, *, days=0, status="scheduled"):
        return Payment.objects.create(
            loan=self.loan,
            amount=Decimal(str(amount)),
            scheduled_date=timezone.localdate() + timedelta(days=days),
            status=status,
            processed_at=timezone.now() if status == "completed" else None,
        )

    def test_dry_run_simulates_without_writing(self):
        pending = self._add("295.00", status="pending")
        stub = self._add("0.01")
        later = self._add("147.18", days=14)

        plan = self.LoanService.heal_upcoming_schedule_keeping_pending(
            self.loan,
            payment_amount=Decimal("147.18"),
            frequency="bi-weekly",
            dry_run=True,
        )

        self.assertTrue(plan["dry_run"])
        self.assertEqual(len(plan["protected"]), 1)
        self.assertEqual(plan["protected"][0]["id"], str(pending.id))
        self.assertEqual(plan["protected_sum"], Decimal("295.00"))
        self.assertEqual(plan["schedule_total"], Decimal("588.09"))
        self.assertGreaterEqual(len(plan["proposed"]), 1)
        self.assertEqual(
            sum((row["amount"] for row in plan["proposed"]), Decimal("0.00")),
            Decimal("588.09"),
        )
        # Nothing written.
        self.assertTrue(Payment.objects.filter(pk=stub.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=later.pk).exists())
        self.assertEqual(Payment.objects.filter(pk=pending.pk).count(), 1)

    def test_apply_keeps_pending_deletes_stub_and_rebuilds_remainder(self):
        pending = self._add("295.00", status="pending")
        stub = self._add("0.01")
        self._add("147.18", days=14)
        self._add("147.18", days=28)
        self._add("147.18", days=42)
        self._add("147.18", days=56)
        self._add("147.19", days=70)

        plan = self.LoanService.heal_upcoming_schedule_keeping_pending(
            self.loan,
            payment_amount=Decimal("147.18"),
            frequency="bi-weekly",
            dry_run=False,
            user=self.staff,
        )

        self.assertFalse(plan["dry_run"])
        pending.refresh_from_db()
        self.assertEqual(pending.amount, Decimal("295.00"))
        self.assertEqual(pending.status, "pending")
        self.assertFalse(Payment.objects.filter(pk=stub.pk).exists())

        scheduled = list(
            self.loan.payments.filter(status="scheduled").order_by(
                "scheduled_date", "created_at", "id"
            )
        )
        self.assertGreaterEqual(len(scheduled), 1)
        scheduled_sum = sum((p.amount for p in scheduled), Decimal("0.00"))
        self.assertEqual(scheduled_sum, Decimal("588.09"))
        self.assertEqual(pending.amount + scheduled_sum, self.loan.total_amount)

        from loans.serializers import CustomerLoanPaymentSerializer

        rows = list(self.loan.payments.exclude(status="cancelled"))
        balances = [
            Decimal(str(row["balance_after"]))
            for row in CustomerLoanPaymentSerializer(rows, many=True).data
        ]
        self.assertEqual(balances[-1], Decimal("0.00"))
        self.assertEqual(balances.count(Decimal("0.00")), 1)
        self.assertTrue(all(b > 0 for b in balances[:-1]), balances)

    def test_apply_preserves_completed_and_processing_collection_payment(self):
        completed = self._add("100.00", days=-14, status="completed")
        self.loan.balance = Decimal("783.09")
        self.loan.save(update_fields=["balance", "updated_at"])
        in_flight = self._add("200.00", status="scheduled")
        CollectionPayment.objects.create(
            loan=self.loan,
            payment=in_flight,
            amount=in_flight.amount,
            status="processing",
        )
        stub = self._add("50.00", days=14)

        plan = self.LoanService.heal_upcoming_schedule_keeping_pending(
            self.loan,
            payment_amount=Decimal("150.00"),
            frequency="bi-weekly",
            dry_run=False,
        )

        self.assertTrue(Payment.objects.filter(pk=completed.pk, status="completed").exists())
        in_flight.refresh_from_db()
        self.assertEqual(in_flight.amount, Decimal("200.00"))
        self.assertFalse(Payment.objects.filter(pk=stub.pk).exists())
        self.assertEqual(plan["protected_sum"], Decimal("200.00"))
        self.assertEqual(plan["schedule_total"], Decimal("583.09"))

    def test_management_command_dry_run_then_apply(self):
        from io import StringIO
        from django.core.management import call_command

        pending = self._add("295.00", status="pending")
        stub = self._add("0.01")
        self._add("588.08", days=14)

        out = StringIO()
        call_command(
            "heal_schedule_keeping_pending",
            f"--loan-id={self.loan.id}",
            "--payment-amount=147.18",
            "--frequency=bi-weekly",
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn("DRY-RUN", text)
        self.assertIn("588.09", text)
        self.assertTrue(Payment.objects.filter(pk=stub.pk).exists())

        out_apply = StringIO()
        call_command(
            "heal_schedule_keeping_pending",
            f"--loan-id={self.loan.id}",
            "--payment-amount=147.18",
            "--frequency=bi-weekly",
            "--apply",
            stdout=out_apply,
        )
        self.assertIn("APPLIED", out_apply.getvalue())
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertFalse(Payment.objects.filter(pk=stub.pk).exists())


@override_settings(ZUMRAILS_DRY_RUN=True)
class BlockedInstitutionFundingTests(APITestCase):
    """Institutions 621 / 623 / 703 warn agents but may still fund/collect."""

    RISK_INSTITUTIONS = ("621", "623", "703")

    def setUp(self):
        self.staff = User.objects.create_user(
            email="blocked-agent@example.com",
            password="password123",
            full_name="Agent User",
            user_type="staff",
            is_staff=True,
        )
        self.customer = Customer.objects.create(
            first_name="Blocked",
            last_name="Institution",
            email="blocked@example.com",
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="pending",
            banking_verified=True,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-blocked",
            sync_status="synced",
        )
        self.allowed_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-allowed",
            name="RBC Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.allowed_account,
            collections_account=self.allowed_account,
            is_active=True,
        )
        self.client.force_authenticate(self.staff)

    def risk_account(self, institution):
        return BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id=f"acct-{institution}",
            name=f"Risk {institution}",
            type="checking",
            transit_number="54321",
            institution_number=institution,
            account_number="9876543210",
        )

    def test_model_flags_risk_institutions(self):
        for institution in self.RISK_INSTITUTIONS:
            with self.subTest(institution=institution):
                self.assertTrue(self.risk_account(institution).is_payment_blocked)
        self.assertFalse(self.allowed_account.is_payment_blocked)

    def test_configuration_endpoint_accepts_risk_funding_account_with_warning(self):
        for institution in self.RISK_INSTITUTIONS:
            with self.subTest(institution=institution):
                account = self.risk_account(institution)
                response = self.client.patch(
                    f"/api/loans/{self.loan.id}/funding/configuration/",
                    {"eft_bank_account_id": str(account.id)},
                    format="json",
                )
                self.assertEqual(response.status_code, 200, response.data)
                self.loan.refresh_from_db()
                self.assertEqual(self.loan.bank_account_id, account.id)
                warnings = funding_configuration_ready(self.loan)["warnings"]
                self.assertTrue(
                    any(institution in warning for warning in warnings),
                    warnings,
                )
                account.delete()
                self.loan.bank_account = self.allowed_account
                self.loan.save(update_fields=["bank_account"])

    def test_configuration_endpoint_accepts_risk_collections_account(self):
        account = self.risk_account("703")
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {"collections_account_id": str(account.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.collections_account_id, account.id)
        readiness = funding_configuration_ready(self.loan)
        self.assertFalse(
            any("703" in blocker for blocker in readiness["blockers"]),
            readiness["blockers"],
        )
        self.assertTrue(
            any("703" in warning for warning in readiness["warnings"]),
            readiness["warnings"],
        )

    def test_configuration_endpoint_still_accepts_allowed_account(self):
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {
                "emt_email": self.customer.email,
                "emt_source": "application",
                "eft_bank_account_id": str(self.allowed_account.id),
                "collections_account_id": str(self.allowed_account.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_funding_readiness_reports_risk_account_as_warning_not_blocker(self):
        risk = self.risk_account("621")
        self.loan.collections_account = risk
        self.loan.save(update_fields=["collections_account"])

        readiness = funding_configuration_ready(self.loan)
        self.assertFalse(
            any("621" in blocker for blocker in readiness["blockers"]),
            readiness["blockers"],
        )
        self.assertTrue(
            any("621" in warning for warning in readiness["warnings"]),
            readiness["warnings"],
        )
        self.assertTrue(
            any("other lenders were able to collect" in warning for warning in readiness["warnings"]),
            readiness["warnings"],
        )

    def test_funding_options_heals_risk_primary_without_destination_blockers(self):
        """703-only customers must get a warning, not destination required blockers."""
        self.allowed_account.is_primary = False
        self.allowed_account.save(update_fields=["is_primary"])
        risk = self.risk_account("703")
        risk.is_primary = True
        risk.use_for_eft_funding = True
        risk.save(update_fields=["is_primary", "use_for_eft_funding", "updated_at"])
        self.loan.bank_account = None
        self.loan.collections_account = None
        self.loan.funding_destination = {}
        self.loan.save(
            update_fields=["bank_account", "collections_account", "funding_destination", "updated_at"]
        )

        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.bank_account_id, risk.id)
        self.assertNotIn("Funding destination required.", response.data["blockers"])
        self.assertNotIn("Collections account required.", response.data["blockers"])
        self.assertTrue(
            any("703" in warning for warning in response.data.get("warnings", [])),
            response.data.get("warnings"),
        )

    def test_funding_initiate_allows_risk_collections_account(self):
        risk = self.risk_account("623")
        self.loan.collections_account = risk
        self.loan.bank_account = risk
        self.loan.funding_destination = {
            "eft": {
                "bank_account_id": str(risk.id),
                "account": {
                    "id": str(risk.id),
                    "institution_number": "623",
                    "transit_number": "54321",
                    "account_number": "9876543210",
                },
            }
        }
        self.loan.save(
            update_fields=["collections_account", "bank_account", "funding_destination"]
        )
        funded = FundingService.initiate(
            loan=self.loan,
            method="eft",
            schedule_confirmed=True,
            user=self.staff,
            collections_account=risk,
        )
        self.assertIsNotNone(funded)
        self.assertTrue(FundedPayment.objects.filter(loan=self.loan).exists())

    def test_collections_account_change_allows_risk_account(self):
        failed_payment = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            status="failed",
            failure_reason="EftFailedInsufficientFunds",
        )
        risk = self.risk_account("703")

        CollectionService.change_account(
            self.loan,
            new_account=risk,
            failed_payment=failed_payment,
            user=self.staff,
        )
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.collections_account_id, risk.id)

    def test_zumrails_user_payload_allows_risk_institutions(self):
        for institution in self.RISK_INSTITUTIONS:
            with self.subTest(institution=institution):
                account = self.risk_account(institution)
                payload = ZumRailsService.user_payload(self.customer, account=account)
                self.assertEqual(
                    payload["BankAccountInformation"]["InstitutionNumber"],
                    institution,
                )
                account.delete()

    def test_zumrails_user_payload_allows_supported_institution(self):
        payload = ZumRailsService.user_payload(self.customer, account=self.allowed_account)
        self.assertEqual(
            payload["BankAccountInformation"],
            {
                "InstitutionNumber": "003",
                "TransitNumber": "12345",
                "AccountNumber": "1234567890",
            },
        )

    def test_zumrails_user_payload_zero_pads_bank_routing(self):
        account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-unpadded",
            name="Unpadded",
            type="checking",
            transit_number="45",
            institution_number="3",
            account_number="999888",
            is_primary=False,
        )
        payload = ZumRailsService.user_payload(self.customer, account=account)
        self.assertEqual(
            payload["BankAccountInformation"],
            {
                "InstitutionNumber": "003",
                "TransitNumber": "00045",
                "AccountNumber": "999888",
            },
        )


@override_settings(ZUMRAILS_DRY_RUN=True)
class FundingFailureRecoveryTests(APITestCase):
    """A failed funding attempt must leave the loan fundable again."""

    def setUp(self):
        self.staff = User.objects.create_user(
            email="manager@example.com",
            password="password123",
            full_name="Manager User",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Retry",
            last_name="Customer",
            email="retry@example.com",
            phone="4165553333",
            phone_normalized="4165553333",
            province="ON",
            status="pending",
            banking_verified=True,
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-retry",
            sync_status="synced",
        )
        self.account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-retry-1",
            name="RBC Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        self.replacement_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-retry-2",
            name="TD Chequing",
            type="checking",
            transit_number="54321",
            institution_number="004",
            account_number="9876543210",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("300.00"),
            fee=Decimal("60.00"),
            total_amount=Decimal("360.00"),
            balance=Decimal("360.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.account,
            collections_account=self.account,
            is_active=True,
        )
        self.client.force_authenticate(self.staff)

    def attempt_funding(self):
        return self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsRequestError(
            "Unable to authenticate with ZūmRails.",
            outcome_unknown=False,
        ),
    )
    def test_failed_attempt_unlocks_destination_for_correction(self, mock_send):
        self.assertEqual(self.attempt_funding().status_code, 502)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.funded_payments.get().status, "failed")
        self.assertIsNone(self.loan.funding_destination_locked_at)
        self.assertIsNone(self.loan.collections_account_locked_at)

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {"eft_bank_account_id": str(self.replacement_account.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.bank_account_id, self.replacement_account.id)

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsRequestError("request outcome unknown"),
    )
    def test_unknown_outcome_keeps_destination_locked(self, mock_send):
        """Money may already be moving, so the destination must stay frozen."""
        self.assertEqual(self.attempt_funding().status_code, 502)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.funded_payments.get().status, "processing")
        self.assertIsNotNone(self.loan.funding_destination_locked_at)
        self.assertIsNotNone(self.loan.collections_account_locked_at)

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsConfigurationError("ZūmRails API is not configured."),
    )
    def test_configuration_error_closes_the_attempt(self, mock_send):
        """A misconfiguration never reached Zūm, so it must not block the retry."""
        self.assertEqual(self.attempt_funding().status_code, 400)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.funded_payments.get().status, "failed")
        self.assertIsNone(self.loan.funding_destination_locked_at)
        self.assertNotIn(
            "Funding already exists for this loan.",
            funding_configuration_ready(self.loan)["blockers"],
        )

    @patch(
        "loans.zumrails.ZumRailsService.initiate_transaction",
        side_effect=ZumRailsRequestError("rejected", outcome_unknown=False),
    )
    def test_failed_attempt_leaves_no_destination_blocker(self, mock_send):
        """The attempt must not erase the configured destination."""
        self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {
                "emt_email": self.customer.email,
                "emt_source": "application",
                "eft_bank_account_id": str(self.account.id),
                "collections_account_id": str(self.account.id),
            },
            format="json",
        )
        self.loan.refresh_from_db()

        self.assertEqual(self.attempt_funding().status_code, 502)

        self.loan.refresh_from_db()
        readiness = funding_configuration_ready(self.loan)
        self.assertTrue(readiness["emt_configured"])
        self.assertTrue(readiness["eft_configured"])
        self.assertEqual(readiness["blockers"], [])

    def test_eft_only_loan_is_not_blocked_on_missing_emt(self):
        """EFT and e-Transfer are alternatives; requiring both blocked funding."""
        readiness = funding_configuration_ready(self.loan)

        self.assertFalse(readiness["emt_configured"])
        self.assertTrue(readiness["eft_configured"])
        self.assertEqual(readiness["blockers"], [])

    def test_webhook_failure_unlocks_destination(self):
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
        )
        self.loan.funding_destination_locked_at = timezone.now()
        self.loan.collections_account_locked_at = timezone.now()
        self.loan.save(update_fields=[
            "funding_destination_locked_at",
            "collections_account_locked_at",
            "updated_at",
        ])

        funding.mark_failed("EftFailedInsufficientFunds")
        _reopen_loan_after_funding_failure(funding)

        self.loan.refresh_from_db()
        self.assertIsNone(self.loan.funding_destination_locked_at)
        self.assertIsNone(self.loan.collections_account_locked_at)

    def test_stale_lock_after_failed_attempt_allows_configuration(self):
        """UI keys off funded-payment status; leftover locks must not win."""
        FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="failed",
            failure_reason="Unable to authenticate with ZūmRails.",
        )
        self.loan.funding_destination_locked_at = timezone.now()
        self.loan.collections_account_locked_at = timezone.now()
        self.loan.funding_destination = {
            "method": "eft",
            "eft": {
                "bank_account_id": str(self.account.id),
                "account": {
                    "id": str(self.account.id),
                    "institution_number": self.account.institution_number,
                    "transit_number": self.account.transit_number,
                    "account_number": self.account.account_number,
                },
            },
        }
        self.loan.save(update_fields=[
            "funding_destination",
            "funding_destination_locked_at",
            "collections_account_locked_at",
            "updated_at",
        ])

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {"eft_bank_account_id": str(self.replacement_account.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertIsNone(self.loan.funding_destination_locked_at)
        self.assertEqual(self.loan.bank_account_id, self.replacement_account.id)


class PaymentPendingDisplayTests(APITestCase):
    """Payment.status=pending is in-flight collection → display Processing."""

    def test_payment_pending_status_display_is_processing(self):
        self.assertEqual(dict(Payment.STATUS_CHOICES)["pending"], "Processing")
        self.assertEqual(dict(Payment.STATUS_CHOICES)["scheduled"], "Scheduled")


class CustomerLoanFrequencyFieldTests(APITestCase):
    """GET customer loans always includes frequency for the schedule badge."""

    def setUp(self):
        self.staff = User.objects.create_user(
            email="freq-api@example.com",
            password="password123",
            full_name="Freq API Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="freq-api-customer@example.com",
            password="password123",
            full_name="Freq API Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Freq",
            last_name="API",
            email="freq-api-customer@example.com",
            phone="4165553333",
            phone_normalized="4165553333",
            province="ON",
            status="active",
            onboarding_stage="portal_active",
            banking_verified=True,
            contract_completed=True,
            requested_loan_amount=Decimal("500.00"),
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

    def test_customer_loans_includes_frequency_from_schedule_gaps(self):
        start = timezone.localdate()
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("200.00"),
            scheduled_date=start,
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("200.00"),
            scheduled_date=start + timedelta(days=14),
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("200.00"),
            scheduled_date=start + timedelta(days=28),
            status="scheduled",
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        row = next(item for item in response.data if item["id"] == str(self.loan.id))
        self.assertIn("frequency", row)
        self.assertEqual(row["frequency"], "bi-weekly")

    def test_customer_loans_frequency_defaults_without_payments(self):
        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        row = next(item for item in response.data if item["id"] == str(self.loan.id))
        self.assertEqual(row["frequency"], "bi-weekly")


@override_settings(ZUMRAILS_DRY_RUN=True)
class ScheduleFrequencyAndDeferralInterestTests(APITestCase):
    """Frequency badge field + daily interest on deferral."""

    def setUp(self):
        from loans.services import LoanService

        self.LoanService = LoanService
        self.staff = User.objects.create_user(
            email="freq-defer@example.com",
            password="password123",
            full_name="Freq Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="freq-customer@example.com",
            password="password123",
            full_name="Freq Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Freq",
            last_name="Customer",
            email="freq-customer@example.com",
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="active",
            onboarding_stage="portal_active",
            banking_verified=True,
            contract_completed=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.formula = LoanFormula.objects.create(
            name="Freq Defer 500",
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
            funded_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.staff)

    def test_customer_loans_api_exposes_schedule_frequency(self):
        start = timezone.localdate()
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start,
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start + timedelta(days=7),
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start + timedelta(days=14),
            status="scheduled",
        )

        response = self.client.get(f"/api/customers/{self.customer.id}/loans/")
        self.assertEqual(response.status_code, 200, response.data)
        loan_row = next(row for row in response.data if row["id"] == str(self.loan.id))
        # Schedule gaps are weekly even though formula default is bi-weekly.
        self.assertEqual(loan_row["frequency"], "weekly")
        self.assertEqual(self.LoanService.schedule_frequency_key(self.loan), "weekly")

    def test_frequency_falls_back_to_formula_without_schedule(self):
        self.assertEqual(self.LoanService.schedule_frequency_key(self.loan), "bi-weekly")

        self.formula.default_frequency_days = 30
        self.formula.save(update_fields=["default_frequency_days"])
        self.assertEqual(self.LoanService.schedule_frequency_key(self.loan), "monthly")

    def test_defer_applies_formula_daily_interest_to_installment(self):
        start = timezone.localdate()
        first = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start,
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start + timedelta(days=14),
            status="scheduled",
        )
        last = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=start + timedelta(days=28),
            status="scheduled",
        )

        # 883.09 * 0.35 / 365 * 14
        expected_interest = self.LoanService.money(
            Decimal("883.09")
            * (Decimal("35.00") / Decimal("100") / Decimal("365"))
            * Decimal("14")
        )
        self.assertEqual(
            self.LoanService._deferral_extra_interest(self.loan, 14),
            expected_interest,
        )
        self.assertGreater(expected_interest, Decimal("0.00"))

        original_balance = self.loan.balance
        original_total = self.loan.total_amount
        response = self.client.post(f"/api/payments/{first.id}/defer/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        first.refresh_from_db()
        self.loan.refresh_from_db()
        last.refresh_from_db()

        self.assertEqual(first.scheduled_date, last.scheduled_date + timedelta(days=14))
        self.assertEqual(first.amount, Decimal("147.18") + expected_interest)
        delta = Decimal("35.00") + expected_interest
        self.assertEqual(self.loan.balance, original_balance + delta)
        self.assertEqual(self.loan.total_amount, original_total + delta)
        self.assertEqual(self.loan.fee, Decimal("383.09") + delta)

        fee = self.loan.payments.get(notes__icontains="Deferral fee")
        self.assertEqual(fee.amount, Decimal("35.00"))
        self.assertEqual(fee.scheduled_date, start)
        # Interest lands on the deferred installment; fee stays a separate $35 row.
        self.assertIn("daily interest", (first.notes or "").lower())
        self.assertEqual(
            first.amount + fee.amount,
            Decimal("147.18") + expected_interest + Decimal("35.00"),
        )


@override_settings(ZUMRAILS_DRY_RUN=True)
class IncompleteBankCoordinatesFundingTests(APITestCase):
    """Block funding when institution / transit / account number is incomplete."""

    def setUp(self):
        self.staff = User.objects.create_user(
            email="coords-agent@example.com",
            password="password123",
            full_name="Coords Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Joseph",
            last_name="Saad",
            email="jsrdconsultants@gmail.com",
            phone="4165559999",
            phone_normalized="4165559999",
            province="ON",
            status="pending",
            banking_verified=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-coords",
            sync_status="synced",
        )
        self.incomplete = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-incomplete",
            name="Tangerine Chequing Account",
            type="checking",
            transit_number="",
            institution_number="",
            account_number="4021897116",
            is_primary=True,
        )
        self.complete = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-complete",
            name="RBC Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=False,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            bank_account=self.incomplete,
            collections_account=self.incomplete,
            funding_destination={
                "emt": {"email": self.customer.email, "source": "application"},
                "eft": {
                    "bank_account_id": str(self.incomplete.id),
                    "account": {
                        "id": str(self.incomplete.id),
                        "institution_number": "",
                        "transit_number": "",
                        "account_number": "4021897116",
                    },
                },
            },
            is_active=True,
        )
        self.client.force_authenticate(self.staff)

    def test_funding_options_blocks_incomplete_coordinates(self):
        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(response.status_code, 200, response.data)
        blockers = " ".join(response.data.get("blockers") or [])
        self.assertIn("institution number", blockers)
        self.assertIn("transit number", blockers)
        self.assertIn("Collections account", blockers)
        self.assertIn("Funding account", blockers)

    def test_funding_initiate_eft_rejects_incomplete_coordinates(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {
                "method": "eft",
                "schedule_confirmed": True,
                "destination": {"bank_account_id": str(self.incomplete.id)},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("institution number", response.data.get("error", "").lower())

    def test_funding_initiate_etransfer_rejects_incomplete_collections(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {
                "method": "etransfer",
                "schedule_confirmed": True,
                "destination": {"email": self.customer.email},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("collections account", response.data.get("error", "").lower())
        self.assertIn("institution number", response.data.get("error", "").lower())

    def test_complete_coordinates_clear_blockers(self):
        self.loan.bank_account = self.complete
        self.loan.collections_account = self.complete
        self.loan.funding_destination = {
            "emt": {"email": self.customer.email, "source": "application"},
            "eft": {
                "bank_account_id": str(self.complete.id),
                "account": {
                    "id": str(self.complete.id),
                    "institution_number": "003",
                    "transit_number": "12345",
                    "account_number": "1234567890",
                },
            },
        }
        self.loan.save(
            update_fields=["bank_account", "collections_account", "funding_destination", "updated_at"]
        )

        readiness = funding_configuration_ready(self.loan)
        self.assertFalse(
            any("missing" in blocker.lower() for blocker in readiness["blockers"]),
            readiness["blockers"],
        )
