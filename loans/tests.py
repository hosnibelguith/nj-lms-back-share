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

from .models import CollectionPayment, CollectionsAccountChangeAudit, FundedPayment, Loan


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
            contract_completed=True,
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
            bank_account=self.account,
            collections_account=self.account,
            is_active=True,
        )
        self.client.force_authenticate(self.staff)

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
        self.assertEqual(CollectionsAccountChangeAudit.objects.filter(loan=self.loan).count(), 1)
