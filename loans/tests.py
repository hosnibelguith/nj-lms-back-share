import base64
import hashlib
import hmac
import json
from decimal import Decimal
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection

from .models import CollectionPayment, CollectionsAccountChangeAudit, FundedPayment, FundingMethodRecommendation, Loan, Payment


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

        self.assertEqual(response.status_code, 400)
        self.assertIn("schedule_confirmed", response.data)

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
        self.assertEqual(self.loan.status, "active")
        self.assertIsNotNone(self.loan.funded_at)

    def test_funding_initiate_activates_loan_immediately(self):
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.loan.refresh_from_db()
        funding = self.loan.funded_payments.order_by("-created_at").first()
        self.assertEqual(funding.status, "processing")
        self.assertEqual(self.loan.status, "active")
        self.assertIsNotNone(self.loan.funded_at)
        self.assertIsNotNone(self.loan.funding_destination_locked_at)
        self.assertIsNotNone(self.loan.collections_account_locked_at)

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
