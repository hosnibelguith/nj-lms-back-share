import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection

from .models import CollectionPayment, CollectionsAccountChangeAudit, FundedPayment, FundingMethodRecommendation, Loan, LoanFormula, LoanStateEvent, Payment
from .zumrails import ZumRailsRequestError, ZumRailsService


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

        self.assertEqual(payments[0].scheduled_date, start_date)
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
        self.assertEqual(response.data["error"], "Funding already exists for this loan.")

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
        self.assertEqual(self.loan.funded_payments.order_by("-created_at").first().status, "processing")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertIsNone(self.loan.funded_at)

    def test_funding_initiate_waits_for_completed_webhook(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        funding = self.loan.funded_payments.order_by("-created_at").first()
        self.assertEqual(funding.status, "processing")
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertIsNone(self.loan.funded_at)
        self.assertIsNotNone(self.loan.funding_destination_locked_at)
        self.assertIsNotNone(self.loan.collections_account_locked_at)

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
        self.assertEqual(duplicate.data["error"], "Funding already exists for this loan.")
        self.assertEqual(mock_send.call_count, 1)

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
        self.assertEqual(collection.status, "failed")
        self.assertEqual(collection.event_history[0]["event"], "EftFailedFundsNotFree")

    def test_collection_completed_webhook_waits_for_settlement(self):
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            status="processing",
            processor_transaction_id="collection-tx-1",
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

    def test_process_collection_settlements_task(self):
        from loans.tasks import process_collection_settlements

        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        collection = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="processing",
            processor_transaction_id="settlement-task-1",
            zum_status="Completed",
            settlement_due_at=timezone.now() - timedelta(minutes=1),
            account_snapshot={"id": str(self.account.id)},
        )

        result = process_collection_settlements()

        collection.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(result["completed"], 1)
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
        self.assertEqual(self.loan.balance, Decimal("600.00"))


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
        self.assertFalse(payload["InteracHasSecurityQuestionAndAnswer"])

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
