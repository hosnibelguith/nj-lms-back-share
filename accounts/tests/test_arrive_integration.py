from decimal import Decimal
from unittest.mock import patch
import json

from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from accounts.arrive_integration import build_decision_payload, sign_arrive_webhook
from accounts.models import Customer, User
from loans.models import Loan
from loans.services import LoanService


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
