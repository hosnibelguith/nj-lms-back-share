from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from banking.ibv_request import IBV_REQUEST_TEMPLATE_NAME
from communications.models import CommunicationTemplate
from loans.models import Loan


@override_settings(FRONTEND_URL="https://app.mohawkloans.com")
class StaffIbvRequestTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="ibv-staff@example.com",
            password="password123",
            full_name="IBV Staff",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="ibv-customer@example.com",
            password="password123",
            full_name="IBV Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Ibv",
            last_name="Customer",
            email="ibv-customer@example.com",
            phone="4165550404",
            province="ON",
            status="active",
            onboarding_stage="banking_verification",
            banking_verified=False,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=500,
            fee=100,
            total_amount=600,
            balance=600,
            status="ibv_pending",
        )
        self.template = CommunicationTemplate.objects.filter(
            name=IBV_REQUEST_TEMPLATE_NAME,
            type="email",
            is_active=True,
        ).first()
        if self.template is None:
            self.template = CommunicationTemplate.objects.create(
                name=IBV_REQUEST_TEMPLATE_NAME,
                type="email",
                trigger="manual",
                subject="Compléter votre demande IBV",
                content="Complete IBV: {{ibv_url}}",
                html_content='<a href="{{ibv_url}}">Fill IBV Request</a>',
                is_active=True,
            )
        self.client.force_authenticate(user=self.staff)

    @patch("communications.tasks.send_template_message.delay")
    def test_staff_can_request_ibv_email(self, send_delay):
        response = self.client.post(
            f"/api/customers/{self.customer.id}/request-ibv/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["ibv_url"],
            "https://app.mohawkloans.com/customer/banking",
        )
        send_delay.assert_called_once()
        args = send_delay.call_args.args
        kwargs = send_delay.call_args.kwargs
        self.assertEqual(args[0], str(self.customer.id))
        self.assertEqual(args[1], str(self.template.id))
        self.assertEqual(args[2], str(self.loan.id))
        self.assertEqual(
            kwargs["extra_context"]["ibv_url"],
            "https://app.mohawkloans.com/customer/banking",
        )
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.ibv_refill_requested)
        self.assertTrue(response.data["ibv_refill_requested"])

    @patch("communications.tasks.send_template_message.delay")
    def test_request_ibv_reopens_onboarding_and_deactivates_flinks(self, send_delay):
        from banking.models import BankConnection

        self.customer.banking_verified = True
        self.customer.ibv_source = Customer.IBV_SOURCE_FLINKS
        self.customer.onboarding_stage = "contract"
        self.customer.save(
            update_fields=[
                "banking_verified",
                "ibv_source",
                "onboarding_stage",
                "updated_at",
            ]
        )
        self.loan.status = "pending_signature"
        self.loan.save(update_fields=["status", "updated_at"])
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="old-ibv-login",
            provider="flinks",
            is_active=True,
            sync_status="synced",
        )

        response = self.client.post(
            f"/api/customers/{self.customer.id}/request-ibv/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        send_delay.assert_called_once()
        self.customer.refresh_from_db()
        self.loan.refresh_from_db()
        connection.refresh_from_db()
        self.assertTrue(self.customer.ibv_refill_requested)
        self.assertFalse(self.customer.banking_verified)
        self.assertEqual(self.customer.ibv_source, "")
        self.assertEqual(self.customer.onboarding_stage, "banking_verification")
        self.assertEqual(self.loan.status, "pending_signature")
        self.assertFalse(connection.is_active)
        self.assertEqual(connection.sync_status, "failed")

        self.client.force_authenticate(user=self.portal_user)
        status = self.client.get("/api/portal/me/banking/")
        self.assertEqual(status.status_code, 200, status.data)
        self.assertTrue(status.data["ibv_refill_requested"])
        self.assertFalse(status.data["banking_verified"])
        self.assertFalse(status.data["has_connection"])

    @patch("communications.tasks.send_template_message.delay")
    def test_request_ibv_keeps_funded_loan_verified(self, send_delay):
        from banking.models import BankConnection

        self.customer.banking_verified = True
        self.customer.ibv_source = Customer.IBV_SOURCE_FLINKS
        self.customer.onboarding_stage = "portal_active"
        self.customer.save(
            update_fields=[
                "banking_verified",
                "ibv_source",
                "onboarding_stage",
                "updated_at",
            ]
        )
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="funded-ibv-login",
            provider="flinks",
            is_active=True,
            sync_status="synced",
        )

        response = self.client.post(
            f"/api/customers/{self.customer.id}/request-ibv/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        send_delay.assert_called_once()
        self.customer.refresh_from_db()
        self.loan.refresh_from_db()
        connection.refresh_from_db()
        self.assertTrue(self.customer.ibv_refill_requested)
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.ibv_source, Customer.IBV_SOURCE_FLINKS)
        self.assertEqual(self.customer.onboarding_stage, "portal_active")
        self.assertEqual(self.loan.status, "active")
        self.assertFalse(connection.is_active)

        self.client.force_authenticate(user=self.portal_user)
        reset = self.client.post("/api/banking/reset-pending/", {}, format="json")
        self.assertEqual(reset.status_code, 200, reset.data)

    def test_mark_ibv_received_advances_client_to_contract(self):
        response = self.client.post(
            f"/api/customers/{self.customer.id}/mark-ibv-received/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.customer.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.ibv_source, Customer.IBV_SOURCE_SYNCDATA)
        self.assertEqual(self.customer.onboarding_stage, "contract")
        self.assertEqual(self.loan.status, "pending_signature")
        self.assertIn("SyncData", response.data["message"])

        again = self.client.post(
            f"/api/customers/{self.customer.id}/mark-ibv-received/",
            {},
            format="json",
        )
        self.assertEqual(again.status_code, 200, again.data)
        self.assertTrue(again.data["already_marked"])
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.ibv_refill_requested)

    def test_mark_ibv_received_clears_staff_ibv_refill(self):
        self.customer.ibv_refill_requested = True
        self.customer.save(update_fields=["ibv_refill_requested", "updated_at"])
        response = self.client.post(
            f"/api/customers/{self.customer.id}/mark-ibv-received/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.ibv_refill_requested)
        self.assertTrue(self.customer.banking_verified)

    def test_flinks_success_clears_staff_ibv_refill(self):
        from banking.models import BankConnection
        from banking.tasks import _mark_banking_success

        self.customer.ibv_refill_requested = True
        self.customer.save(update_fields=["ibv_refill_requested", "updated_at"])
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="refill-success",
            provider="flinks",
            is_active=True,
            sync_status="pending",
        )
        _mark_banking_success(connection, self.customer)
        self.customer.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertFalse(self.customer.ibv_refill_requested)
        self.assertTrue(self.customer.banking_verified)
        self.assertEqual(self.customer.ibv_source, Customer.IBV_SOURCE_FLINKS)
        self.assertEqual(self.loan.status, "pending_signature")

    def test_mark_ibv_received_blocked_when_flinks_accounts_exist(self):
        from banking.models import BankAccount, BankConnection

        self.customer.banking_verified = True
        self.customer.ibv_source = Customer.IBV_SOURCE_FLINKS
        self.customer.save(update_fields=["banking_verified", "ibv_source", "updated_at"])
        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="ibv-login",
            provider="flinks",
            sync_status="synced",
        )
        BankAccount.objects.create(
            connection=connection,
            customer=self.customer,
            external_id="ibv-acct",
            name="Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567",
        )
        response = self.client.post(
            f"/api/customers/{self.customer.id}/mark-ibv-received/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.ibv_source, Customer.IBV_SOURCE_FLINKS)
