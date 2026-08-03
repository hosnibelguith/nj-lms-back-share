from decimal import Decimal
from unittest.mock import patch
import json

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.arrive_integration import (
    build_decision_payload,
    build_funding_payload,
    sign_arrive_webhook,
)
from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection
from loans.models import Loan
from loans.services import LoanService
from loans.zumrails import FundingService


@override_settings(
    ARRIVE_API_KEY="test-arrive-key",
    ARRIVE_WEBHOOK_SECRET="test-webhook-secret",
    ARRIVE_WEBHOOK_URL="https://example.test/webhooks/lendstack/decision/",
    ARRIVE_PORTAL_BASE_URL="https://portal.test",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ArriveIntegrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {"HTTP_AUTHORIZATION": "Bearer test-arrive-key"}

    def _lead_payload(self, **overrides):
        data = {
            "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "arrive_application_id": "arrive-application-uuid",
            "zum_user_id": "zum-user-1",
            "email": "arrive.customer@example.com",
            "phone": "+14165550100",
            "first_name": "Mohamed",
            "last_name": "Ghanem",
            "requested_loan_amount": "750.00",
            "province": "ON",
        }
        data.update(overrides)
        return data

    def test_create_lead_and_idempotent_retry(self):
        response = self.client.post(
            "/api/integrations/arrive/leads/",
            self._lead_payload(),
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("application_url", body)
        self.assertEqual(body["status"], "application_in_progress")
        self.assertEqual(body["arrive_application_id"], "arrive-application-uuid")
        self.assertEqual(body["requested_amount"], "750.00")
        self.assertIsNone(body["approved_amount"])
        self.assertEqual(body["currency"], "CAD")
        self.assertTrue(
            body["application_url"].startswith(
                "https://portal.test/customer/arrive/handoff?token="
            )
        )

        customer = Customer.objects.get(email="arrive.customer@example.com")
        self.assertEqual(customer.source, Customer.SOURCE_ARRIVE)
        self.assertEqual(customer.arrive_zum_user_id, "zum-user-1")

        retry = self.client.post(
            "/api/integrations/arrive/leads/",
            self._lead_payload(),
            format="json",
            **self.headers,
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["lendstack_customer_id"], body["lendstack_customer_id"])
        self.assertEqual(retry.json()["loan_id"], body["loan_id"])
        self.assertNotEqual(retry.json()["application_url"], body["application_url"])
        self.assertEqual(Customer.objects.filter(email="arrive.customer@example.com").count(), 1)
        self.assertEqual(Loan.objects.filter(customer=customer).count(), 1)

    def test_create_lead_requires_api_key(self):
        response = self.client.post(
            "/api/integrations/arrive/leads/",
            self._lead_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_handoff_exchange_and_portal_session(self):
        created = self.client.post(
            "/api/integrations/arrive/leads/",
            self._lead_payload(),
            format="json",
            **self.headers,
        ).json()
        token = created["application_url"].split("token=")[1]

        handoff = self.client.post(
            "/api/portal/arrive/handoff/",
            {"token": token},
            format="json",
        )
        self.assertEqual(handoff.status_code, 200)
        self.assertIn("access", handoff.json())
        self.assertTrue(handoff.json()["embed_mode"])

        reused = self.client.post(
            "/api/portal/arrive/handoff/",
            {"token": token},
            format="json",
        )
        self.assertEqual(reused.status_code, 400)

        resume = self.client.post(
            "/api/integrations/arrive/portal-session/",
            {
                "arrive_application_id": "arrive-application-uuid",
                "zum_user_id": "zum-user-1",
                "loan_id": created["loan_id"],
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resume.status_code, 200)
        self.assertIn("portal_embed_url", resume.json())
        self.assertEqual(resume.json()["status"], "application_in_progress")
        self.assertEqual(resume.json()["requested_amount"], "750.00")
        self.assertIsNone(resume.json()["approved_amount"])
        self.assertEqual(resume.json()["currency"], "CAD")

    def test_decision_payload_decline_reasons(self):
        user = User(email="x@example.com", full_name="X", user_type="customer")
        user.set_unusable_password()
        user.save()
        customer = Customer.objects.create(
            portal_user=user,
            first_name="X",
            last_name="Y",
            email="x@example.com",
            phone="+14165550999",
            phone_normalized="+14165550999",
            source=Customer.SOURCE_ARRIVE,
            arrive_application_id="app-2",
            arrive_zum_user_id="zum-2",
            requested_loan_amount=Decimal("500.00"),
        )
        loan = LoanService.create_initial_application(customer)
        loan.decline_reason = "Income could not be verified.\nRefs incomplete."
        loan.save(update_fields=["decline_reason"])
        payload = build_decision_payload(loan, decision="declined")
        self.assertEqual(payload["decision"], "declined")
        self.assertIsNone(payload["approved_amount"])
        self.assertEqual(len(payload["decline_reasons"]), 2)


@override_settings(
    ARRIVE_API_KEY="test-arrive-key",
    ARRIVE_WEBHOOK_SECRET="test-webhook-secret",
    ARRIVE_WEBHOOK_URL="https://example.test/webhooks/lendstack/decision/",
    ARRIVE_PORTAL_BASE_URL="https://portal.test",
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ArriveWebhookDeliveryTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {"HTTP_AUTHORIZATION": "Bearer test-arrive-key"}

    @patch("accounts.arrive_integration.requests.post")
    def test_approve_sends_signed_decision_webhook(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        created = self.client.post(
            "/api/integrations/arrive/leads/",
            {
                "event_id": "b1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "arrive_application_id": "arrive-application-uuid-2",
                "zum_user_id": "zum-user-2",
                "email": "arrive2.customer@example.com",
                "phone": "+14165550101",
                "first_name": "Mohamed",
                "last_name": "Ghanem",
                "requested_loan_amount": "750.00",
                "province": "ON",
            },
            format="json",
            **self.headers,
        ).json()
        loan = Loan.objects.get(id=created["loan_id"])
        loan.status = "pending"
        loan.save(update_fields=["status"])
        LoanService.approve_loan(loan)

        self.assertTrue(mock_post.called)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://example.test/webhooks/lendstack/decision/")
        headers = kwargs["headers"]
        self.assertIn("X-LendStack-Signature", headers)
        self.assertIn("X-LendStack-Timestamp", headers)
        raw_body = kwargs["data"]
        expected = sign_arrive_webhook(
            raw_body,
            headers["X-LendStack-Timestamp"],
            "test-webhook-secret",
        )
        self.assertEqual(headers["X-LendStack-Signature"], expected)

        payload = json.loads(raw_body.decode("utf-8"))
        self.assertEqual(payload["decision"], "approved")
        self.assertEqual(payload["approved_amount"], f"{loan.principal:.2f}")
        self.assertEqual(payload["currency"], "CAD")
        self.assertEqual(payload["arrive_application_id"], "arrive-application-uuid-2")


@override_settings(
    ARRIVE_WEBHOOK_SECRET="test-webhook-secret",
    ARRIVE_WEBHOOK_URL="https://example.test/webhooks/lendstack/decision/",
    ARRIVE_FUNDING_WEBHOOK_URL="https://example.test/webhooks/lendstack/funding/",
    ZUMRAILS_DRY_RUN=True,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ArriveFundingWebhookTests(TransactionTestCase):
    """Approval and card funding must reach Arrive as two independent events."""

    def setUp(self):
        self.staff = User.objects.create_user(
            email="funding.agent@example.com",
            password="password123",
            full_name="Funding Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )

    def _arrive_loan(self, *, card_id=None, source=Customer.SOURCE_ARRIVE):
        portal_user = User(
            email="arrive.funding@example.com",
            full_name="Arrive Funding",
            user_type="customer",
        )
        portal_user.set_unusable_password()
        portal_user.save()
        customer = Customer.objects.create(
            portal_user=portal_user,
            first_name="Arrive",
            last_name="Funding",
            email="arrive.funding@example.com",
            phone="+14165550777",
            phone_normalized="+14165550777",
            province="ON",
            source=source,
            arrive_application_id="arrive-application-uuid-fund",
            arrive_zum_user_id="zum-user-fund",
            arrive_zum_user_card_id=card_id,
            requested_loan_amount=Decimal("300.00"),
        )
        connection = BankConnection.objects.create(
            customer=customer,
            login_id="login-fund",
            sync_status="synced",
        )
        account = BankAccount.objects.create(
            connection=connection,
            customer=customer,
            external_id="acct-fund",
            name="RBC Chequing",
            type="checking",
            balance=Decimal("1000.00"),
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        loan = LoanService.create_initial_application(customer)
        loan.principal = Decimal("300.00")
        loan.status = "pending_funding"
        loan.contract_signed_at = timezone.now()
        loan.bank_account = account
        loan.collections_account = account
        loan.save()
        return loan

    def _fund(self, loan):
        return FundingService.initiate(
            loan,
            method="card_issuance",
            schedule_confirmed=True,
            user=self.staff,
            collections_account=loan.collections_account,
        )

    @staticmethod
    def _calls_to(mock_post, url):
        return [call for call in mock_post.call_args_list if call.args[0] == url]

    @patch("accounts.arrive_integration.requests.post")
    def test_approval_alone_does_not_authorize_funding(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        loan = self._arrive_loan()
        loan.status = "pending"
        loan.save(update_fields=["status"])
        LoanService.approve_loan(loan)

        decision_calls = self._calls_to(
            mock_post, "https://example.test/webhooks/lendstack/decision/"
        )
        funding_calls = self._calls_to(
            mock_post, "https://example.test/webhooks/lendstack/funding/"
        )
        self.assertEqual(len(decision_calls), 1)
        self.assertEqual(funding_calls, [])

    @patch("accounts.arrive_integration.requests.post")
    def test_card_issuance_sends_signed_funding_webhook(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        loan = self._arrive_loan(card_id="zum-card-1")
        funding = self._fund(loan)

        calls = self._calls_to(
            mock_post, "https://example.test/webhooks/lendstack/funding/"
        )
        self.assertEqual(len(calls), 1)

        kwargs = calls[0].kwargs
        headers = kwargs["headers"]
        raw_body = kwargs["data"]
        self.assertEqual(
            headers["X-LendStack-Signature"],
            sign_arrive_webhook(
                raw_body, headers["X-LendStack-Timestamp"], "test-webhook-secret"
            ),
        )

        payload = json.loads(raw_body.decode("utf-8"))
        self.assertEqual(payload["event"], "loan.funding")
        self.assertEqual(payload["arrive_application_id"], "arrive-application-uuid-fund")
        self.assertEqual(payload["lendstack_customer_id"], str(loan.customer_id))
        self.assertEqual(payload["loan_id"], str(loan.id))
        self.assertEqual(payload["zum_user_id"], "zum-user-fund")
        self.assertEqual(payload["card_id"], "zum-card-1")
        self.assertEqual(payload["funding_amount"], "300.00")
        self.assertEqual(payload["currency"], "CAD")
        self.assertEqual(payload["funding_reference_id"], str(funding.id))
        self.assertTrue(payload["funding_authorized_at"].endswith("Z"))

    @patch("accounts.arrive_integration.requests.post")
    def test_card_id_is_optional(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        loan = self._arrive_loan()
        self._fund(loan)

        calls = self._calls_to(
            mock_post, "https://example.test/webhooks/lendstack/funding/"
        )
        payload = json.loads(calls[0].kwargs["data"].decode("utf-8"))
        self.assertIsNone(payload["card_id"])

    def test_funding_event_id_is_stable_across_retries(self):
        loan = self._arrive_loan()
        with patch("accounts.arrive_integration.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = "ok"
            funding = self._fund(loan)

        first = build_funding_payload(loan, funding)
        second = build_funding_payload(loan, funding)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first, second)

    @patch("accounts.arrive_integration.requests.post")
    def test_non_arrive_loan_is_never_authorized(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = "ok"

        loan = self._arrive_loan(source=Customer.SOURCE_ORGANIC)
        with self.assertRaises(ValueError):
            self._fund(loan)

        self.assertEqual(
            self._calls_to(mock_post, "https://example.test/webhooks/lendstack/funding/"),
            [],
        )
