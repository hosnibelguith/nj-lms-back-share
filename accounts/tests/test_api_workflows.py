from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from unittest.mock import patch

from accounts.models import AuthOTPChallenge, Customer, User
from loans.models import FundedPayment, Loan, LoanStateEvent, Payment


@override_settings(
    DEBUG=True,
    DEV_OTP_CODE="123456",
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class BackendApiWorkflowTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="staff@example.com",
            password="Password123!",
            full_name="Staff User",
            user_type="staff",
            permission_level=5,
            is_staff=True,
        )
        self.customer_user = User.objects.create_user(
            email="customer@example.com",
            password="Password123!",
            full_name="Customer User",
            user_type="customer",
            phone="4165550100",
            phone_normalized="+14165550100",
        )
        self.customer = Customer.objects.create(
            portal_user=self.customer_user,
            first_name="Customer",
            last_name="User",
            email="customer@example.com",
            phone="4165550100",
            phone_normalized="+14165550100",
            province="ON",
            status="active",
            onboarding_stage="banking_verification",
            requested_loan_amount=Decimal("500.00"),
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending",
            is_active=True,
        )

    def test_staff_login_me_and_logout_workflow(self):
        login_response = self.client.post(
            "/api/auth/login/",
            {"email": "staff@example.com", "password": "Password123!"},
            format="json",
        )

        self.assertEqual(login_response.status_code, 200, login_response.data)
        self.assertEqual(login_response.data["user"]["user_type"], "staff")
        self.assertIn(settings.AUTH_COOKIE_ACCESS, self.client.cookies)
        self.assertIn(settings.AUTH_COOKIE_REFRESH, self.client.cookies)

        me_response = self.client.get("/api/auth/me/")
        self.assertEqual(me_response.status_code, 200, me_response.data)
        self.assertEqual(me_response.data["email"], "staff@example.com")

        logout_response = self.client.post("/api/auth/logout/", {}, format="json")
        self.assertEqual(logout_response.status_code, 200, logout_response.data)

    def test_customer_signup_otp_verification_creates_customer_user_and_initial_loan(self):
        payload = {
            "first_name": "New",
            "last_name": "Applicant",
            "email": "new-applicant@example.com",
            "phone": "(416) 555-0199",
            "province": "BC",
            "date_of_birth": "1990-01-01",
            "requested_loan_amount": "700.00",
            "password": "Password123!",
            "confirm_password": "Password123!",
        }

        with patch("accounts.serializers.send_sms_otp_task.delay") as sms_mock:
            start_response = self.client.post("/api/portal/signup/start/", payload, format="json")

        self.assertEqual(start_response.status_code, 200, start_response.data)
        self.assertEqual(start_response.data["action"], "verify_phone")
        self.assertTrue(start_response.data["challenge_id"])
        sms_mock.assert_called_once()

        with patch("accounts.views.send_welcome_email.delay") as welcome_mock:
            verify_response = self.client.post(
                "/api/portal/signup/verify-phone/",
                {
                    "challenge_id": start_response.data["challenge_id"],
                    "code": "123456",
                },
                format="json",
            )

        self.assertEqual(verify_response.status_code, 201, verify_response.data)
        self.assertEqual(verify_response.data["customer"]["email"], "new-applicant@example.com")
        welcome_mock.assert_called_once()

        customer = Customer.objects.get(email="new-applicant@example.com")
        self.assertEqual(customer.phone_normalized, "+14165550199")
        self.assertTrue(customer.phone_verified)
        self.assertEqual(customer.loans.count(), 1)
        self.assertEqual(customer.loans.first().status, "ibv_pending")

        challenge = AuthOTPChallenge.objects.get(id=start_response.data["challenge_id"])
        self.assertEqual(challenge.status, AuthOTPChallenge.STATUS_USED)

    def test_existing_customer_signup_start_returns_existing_customer_action(self):
        payload = {
            "first_name": "Customer",
            "last_name": "User",
            "email": "customer@example.com",
            "phone": "4165550100",
            "province": "ON",
            "date_of_birth": "1990-01-01",
            "requested_loan_amount": "500.00",
            "password": "Password123!",
            "confirm_password": "Password123!",
        }

        response = self.client.post("/api/portal/signup/start/", payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["action"], "existing_customer")
        self.assertTrue(response.data["existing_account"])

    def test_customer_login_me_dashboard_and_job_references_workflow(self):
        login_response = self.client.post(
            "/api/portal/login/",
            {"email": "customer@example.com", "password": "Password123!"},
            format="json",
        )
        self.assertEqual(login_response.status_code, 200, login_response.data)

        me_response = self.client.get("/api/portal/me/")
        self.assertEqual(me_response.status_code, 200, me_response.data)
        self.assertEqual(me_response.data["email"], "customer@example.com")

        dashboard_response = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard_response.status_code, 200, dashboard_response.data)
        self.assertIn("portal_state", dashboard_response.data)
        self.assertIn("next_step", dashboard_response.data)
        self.assertIn("banking", dashboard_response.data)

        references_response = self.client.patch(
            "/api/portal/me/job-references/",
            {
                "job_place_name": "Acme Inc",
                "supervisor_name": "Sam Manager",
                "supervisor_phone": "4165550111",
                "reference_1_name": "Pat Reference",
                "reference_1_phone": "4165550222",
                "reference_2_name": "Lee Reference",
                "reference_2_phone": "4165550333",
            },
            format="json",
        )
        self.assertEqual(references_response.status_code, 200, references_response.data)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.references_completed)

    def test_customer_password_reset_workflow_changes_password(self):
        with patch("accounts.serializers.send_email_otp_task.delay") as email_mock:
            request_response = self.client.post(
                "/api/portal/password-reset/request/",
                {"email": "customer@example.com"},
                format="json",
            )

        self.assertEqual(request_response.status_code, 200, request_response.data)
        self.assertTrue(request_response.data["challenge_id"])
        email_mock.assert_called_once()

        verify_response = self.client.post(
            "/api/portal/password-reset/verify/",
            {"challenge_id": request_response.data["challenge_id"], "code": "123456"},
            format="json",
        )
        self.assertEqual(verify_response.status_code, 200, verify_response.data)

        confirm_response = self.client.post(
            "/api/portal/password-reset/confirm/",
            {
                "challenge_id": request_response.data["challenge_id"],
                "password": "NewPassword123!",
                "confirm_password": "NewPassword123!",
            },
            format="json",
        )
        self.assertEqual(confirm_response.status_code, 200, confirm_response.data)

        login_response = self.client.post(
            "/api/portal/login/",
            {"email": "customer@example.com", "password": "NewPassword123!"},
            format="json",
        )
        self.assertEqual(login_response.status_code, 200, login_response.data)

    def test_staff_routes_reject_anonymous_and_customer_users(self):
        anonymous_response = self.client.get("/api/customers/")
        self.assertEqual(anonymous_response.status_code, 401)

        self.client.force_authenticate(user=self.customer_user)
        customer_response = self.client.get("/api/customers/")
        self.assertEqual(customer_response.status_code, 403)

    def test_staff_customer_filters_search_status_province_and_has_loans(self):
        Customer.objects.create(
            first_name="Morgan",
            last_name="Chen",
            email="morgan@example.com",
            phone="6045550100",
            phone_normalized="+16045550100",
            province="BC",
            status="collections",
        )
        self.client.force_authenticate(user=self.staff)

        search_response = self.client.get("/api/customers/", {"search": "Customer"})
        self.assertEqual(search_response.status_code, 200, search_response.data)
        self.assertEqual(search_response.data["count"], 1)
        self.assertEqual(search_response.data["results"][0]["email"], "customer@example.com")

        filtered_response = self.client.get(
            "/api/customers/",
            {"status": "collections", "province": "BC", "has_loans": "false"},
        )
        self.assertEqual(filtered_response.status_code, 200, filtered_response.data)
        self.assertEqual(filtered_response.data["count"], 1)
        self.assertEqual(filtered_response.data["results"][0]["email"], "morgan@example.com")

    def test_staff_loan_filters_and_dashboard_analytics_shape(self):
        LoanStateEvent.objects.create(
            loan=self.loan,
            event_type="human_approved",
            previous_status="pending",
            new_status="pending_funding",
            created_by=self.staff,
        )
        FundedPayment.objects.create(
            loan=self.loan,
            amount=Decimal("500.00"),
            method="eft",
            status="completed",
            initiated_at=timezone.now(),
            completed_at=timezone.now(),
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("100.00"),
            scheduled_date=timezone.localdate(),
            status="completed",
            processed_at=timezone.now(),
        )

        self.client.force_authenticate(user=self.staff)

        list_response = self.client.get(
            "/api/loans/",
            {"search": "Customer", "status": "pending", "province": "ON"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.data)
        results = list_response.data["results"] if isinstance(list_response.data, dict) else list_response.data
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["customer_name"], "Customer User")
        self.assertEqual(results[0]["ibv_status"], "pending")
        self.assertFalse(results[0]["contract_signed"])

        self.loan.ai_decision = "approved"
        self.loan.contract_signed_at = timezone.now()
        self.loan.save(update_fields=["ai_decision", "contract_signed_at", "updated_at"])
        self.customer.banking_verified = True
        self.customer.save(update_fields=["banking_verified", "updated_at"])

        filtered_response = self.client.get(
            "/api/loans/",
            {"ai_decision": "approved", "ibv_status": "completed"},
        )
        self.assertEqual(filtered_response.status_code, 200, filtered_response.data)
        filtered_results = filtered_response.data["results"] if isinstance(filtered_response.data, dict) else filtered_response.data
        self.assertEqual(len(filtered_results), 1)
        self.assertEqual(filtered_results[0]["ai_decision"], "approved")
        self.assertEqual(filtered_results[0]["ibv_status"], "completed")
        self.assertTrue(filtered_results[0]["contract_signed"])

        analytics_response = self.client.get("/api/loans/dashboard/analytics/")
        self.assertEqual(analytics_response.status_code, 200, analytics_response.data)
        self.assertIn("totals", analytics_response.data)
        self.assertIn("series", analytics_response.data)
        self.assertIn("funded_payments_amount", analytics_response.data["series"])

    def test_staff_can_update_approved_amount_before_funding(self):
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("300.00"),
            scheduled_date=timezone.localdate(),
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("300.00"),
            scheduled_date=timezone.localdate() + timedelta(days=14),
            status="scheduled",
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            f"/api/loans/{self.loan.id}/approved-amount/",
            {"principal": "300.00", "notes": "Approved lower amount."},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.principal, Decimal("300.00"))
        self.assertEqual(self.loan.fee, Decimal("0.00"))
        self.assertEqual(self.loan.total_amount, Decimal("300.00"))
        self.assertEqual(self.loan.balance, Decimal("300.00"))
        self.assertEqual(self.loan.payments.filter(status="scheduled").count(), 1)
        self.assertTrue(
            self.loan.state_events.filter(event_type="amount_updated").exists()
        )
