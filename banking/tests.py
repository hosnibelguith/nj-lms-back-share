from unittest.mock import MagicMock, patch

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.models import BankAccount, BankConnection
from banking.tasks import fetch_flinks_accounts_only


def _flinks_accounts_payload():
    return {
        "Accounts": [
            {
                "Id": "acc-1",
                "Title": "Chequing",
                "Type": "Chequing",
                "Currency": "CAD",
                "Balance": {"Current": 1000},
                "InstitutionNumber": "001",
                "TransitNumber": "12345",
                "AccountNumber": "1234567",
                "Holder": {
                    "Name": "Test User",
                    "Email": "test@example.com",
                    "PhoneNumber": "4165551234",
                },
                "Transactions": [
                    {
                        "Id": "tx-1",
                        "Date": "2024-01-01",
                        "Description": "Payroll",
                        "Debit": None,
                        "Credit": "100.00",
                        "Balance": "1000.00",
                    }
                ],
            }
        ]
    }


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    FLINKS_INSTANCE="toolbox",
    FLINKS_CUSTOMER_ID="test-customer-id",
    FLINKS_SECRET_KEY_CA="test-secret",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class BankingConnectWorkflowTests(APITestCase):
    def setUp(self):
        self.portal_user = User.objects.create_user(
            email="banking-customer@example.com",
            password="password123",
            full_name="Banking Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Banking",
            last_name="Customer",
            email="banking-customer@example.com",
            phone="4165551111",
            phone_normalized="4165551111",
            province="ON",
            status="pending",
            onboarding_stage="banking_verification",
            banking_verified=False,
        )
        self.client.force_authenticate(user=self.portal_user)

    def test_connect_returns_200_when_flinks_not_configured(self):
        with override_settings(FLINKS_SECRET_KEY_CA=""):
            response = self.client.post(
                "/api/banking/connect/",
                {"login_id": "demo-login-1"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "SYNCING")

        connection = BankConnection.objects.get(customer=self.customer)
        self.assertEqual(connection.login_id, "demo-login-1")
        self.assertEqual(connection.sync_status, "failed")
        self.assertTrue(connection.sync_error)

        self.customer.refresh_from_db()
        self.assertFalse(self.customer.banking_verified)

    def test_connect_returns_200_when_flinks_auth_fails(self):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("banking.tasks.requests.post", return_value=mock_response):
            response = self.client.post(
                "/api/banking/connect/",
                {"login_id": "demo-login-2"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        connection = BankConnection.objects.get(login_id="demo-login-2")
        self.assertEqual(connection.sync_status, "failed")
        self.assertIn("Unauthorized", connection.sync_error)

    def test_connect_syncs_accounts_and_verifies_banking(self):
        auth_response = MagicMock()
        auth_response.status_code = 200
        auth_response.json.return_value = {"RequestId": "req-123"}

        accounts_response = MagicMock()
        accounts_response.status_code = 200
        accounts_response.json.return_value = _flinks_accounts_payload()

        with patch(
            "banking.tasks.requests.post",
            side_effect=[auth_response, accounts_response],
        ):
            response = self.client.post(
                "/api/banking/connect/",
                {"login_id": "demo-login-3"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)

        connection = BankConnection.objects.get(login_id="demo-login-3")
        self.assertEqual(connection.sync_status, "synced")

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.onboarding_stage, "contract")
        self.assertEqual(BankAccount.objects.filter(customer=self.customer).count(), 1)

        status_response = self.client.get("/api/portal/me/banking/")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.data["banking_verified"])
        self.assertEqual(status_response.data["connection_status"], "synced")
        self.assertEqual(status_response.data["account_count"], 1)

    def test_connect_reuses_login_id_for_same_customer(self):
        BankConnection.objects.create(
            customer=self.customer,
            login_id="demo-login-4",
            sync_status="failed",
        )

        with override_settings(FLINKS_SECRET_KEY_CA=""):
            response = self.client.post(
                "/api/banking/connect/",
                {"login_id": "demo-login-4"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(BankConnection.objects.filter(customer=self.customer).count(), 1)

    def test_connect_replaces_conflicting_demo_login_id(self):
        other_user = User.objects.create_user(
            email="other-customer@example.com",
            password="password123",
            full_name="Other Customer",
            user_type="customer",
        )
        other_customer = Customer.objects.create(
            portal_user=other_user,
            first_name="Other",
            last_name="Customer",
            email="other-customer@example.com",
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="pending",
            onboarding_stage="banking_verification",
            banking_verified=False,
        )
        BankConnection.objects.create(
            customer=other_customer,
            login_id="shared-demo-login",
            sync_status="failed",
        )

        with override_settings(FLINKS_SECRET_KEY_CA=""):
            response = self.client.post(
                "/api/banking/connect/",
                {"login_id": "shared-demo-login"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            BankConnection.objects.filter(
                customer=self.customer,
                login_id="shared-demo-login",
            ).exists()
        )
        self.assertFalse(
            BankConnection.objects.filter(
                customer=other_customer,
                login_id="shared-demo-login",
            ).exists()
        )

    def test_fetch_task_handles_unexpected_errors_without_raising(self):
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="demo-login-5",
            sync_status="pending",
        )

        with patch(
            "banking.tasks._run_flinks_sync",
            side_effect=RuntimeError("unexpected"),
        ):
            result = fetch_flinks_accounts_only("demo-login-5")

        self.assertFalse(result)
        connection.refresh_from_db()
        self.assertEqual(connection.sync_status, "failed")
        self.assertIn("unexpected", connection.sync_error)
