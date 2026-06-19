from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Customer, User
from banking.models import BankConnection
from banking.tasks import UNSUPPORTED_INSTITUTION_MESSAGE, fetch_flinks_accounts_only


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class BankingConnectWorkflowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
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
            phone="4165552222",
            phone_normalized="4165552222",
            province="ON",
            status="pending",
            onboarding_stage="banking_verification",
            banking_verified=True,
        )
        self.client.force_authenticate(self.portal_user)
        self.existing_login_id = "shared-flinks-login-id"

        other_user = User.objects.create_user(
            email="other-customer@example.com",
            password="password123",
            full_name="Other Customer",
            user_type="customer",
        )
        BankConnection.objects.create(
            customer=Customer.objects.create(
                portal_user=other_user,
                first_name="Other",
                last_name="Customer",
                email="other-customer@example.com",
                phone="4165553333",
                phone_normalized="4165553333",
                province="ON",
                status="pending",
            ),
            login_id=self.existing_login_id,
            provider="flinks",
            sync_status="synced",
        )

    @patch("banking.views.fetch_flinks_accounts_only.delay")
    def test_connect_accepts_duplicate_login_id_for_another_customer(self, mock_delay):
        response = self.client.post(
            "/api/banking/connect/",
            {"login_id": self.existing_login_id},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        connection = BankConnection.objects.get(customer=self.customer)
        self.assertEqual(connection.login_id, self.existing_login_id)
        self.assertEqual(
            BankConnection.objects.filter(login_id=self.existing_login_id).count(),
            2,
        )
        mock_delay.assert_called_once_with(str(connection.id))

    @patch("banking.views.fetch_flinks_accounts_only.delay")
    def test_connect_resets_banking_verified_while_sync_starts(self, mock_delay):
        response = self.client.post(
            "/api/banking/connect/",
            {"login_id": "fresh-login-id"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.banking_verified)
        self.assertEqual(self.customer.onboarding_stage, "banking_verification")

    @patch("banking.tasks.requests.post")
    def test_unsupported_institution_621_requires_redo(self, mock_post):
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-621",
            provider="flinks",
            sync_status="pending",
        )

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"RequestId": "req-621"}

            text = ""

        class AccountsResponse:
            status_code = 200

            def json(self):
                return {
                    "Accounts": [
                        {
                            "Id": "acct-621",
                            "Title": "Unsupported Account",
                            "Type": "Chequing",
                            "InstitutionNumber": "621",
                            "Transactions": [{"Id": "tx-1"}],
                        }
                    ]
                }

            text = ""

        mock_post.side_effect = [FakeResponse(), AccountsResponse()]

        result = fetch_flinks_accounts_only(str(connection.id))

        self.assertFalse(result)
        connection.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(connection.sync_status, "failed")
        self.assertEqual(connection.sync_error, UNSUPPORTED_INSTITUTION_MESSAGE)
        self.assertFalse(self.customer.banking_verified)
