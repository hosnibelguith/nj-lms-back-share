from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from unittest.mock import patch

from accounts.models import AuthOTPChallenge, Customer, GlobalSetting, User
from communications.models import Communication, CommunicationTemplate
from communications.tasks import send_loan_workflow_reminders
from contracts.models import Contract
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
        self.assertIn("received_applications_count", analytics_response.data["totals"])
        self.assertIn("received_arrive_count", analytics_response.data["totals"])
        self.assertIn("received_organic_count", analytics_response.data["totals"])

    def test_dashboard_ops_stats_by_day_and_source(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)

        arrive_user = User.objects.create_user(
            email="arrive-dash@example.com",
            password="password123",
            full_name="Arrive Dash",
            user_type="customer",
        )
        arrive_customer = Customer.objects.create(
            portal_user=arrive_user,
            first_name="Arrive",
            last_name="Dash",
            email="arrive-dash@example.com",
            phone="4165559999",
            phone_normalized="4165559999",
            province="ON",
            status="pending",
            source="arrive",
            onboarding_stage="portal_active",
            banking_verified=True,
            requested_loan_amount=Decimal("400.00"),
        )
        arrive_loan = Loan.objects.create(
            customer=arrive_customer,
            principal=Decimal("400.00"),
            fee=Decimal("40.00"),
            total_amount=Decimal("440.00"),
            balance=Decimal("440.00"),
            status="pending_funding",
            is_active=True,
            approved_at=timezone.now(),
        )
        # Keep created_at as today (auto_now_add). Mark organic loan as older.
        self.loan.created_at = timezone.now() - timedelta(days=1)
        self.loan.approved_at = timezone.now() - timedelta(days=1)
        self.loan.funded_at = timezone.now()
        self.loan.status = "active"
        self.loan.save(
            update_fields=[
                "created_at",
                "approved_at",
                "funded_at",
                "status",
                "updated_at",
            ]
        )

        self.client.force_authenticate(user=self.staff)
        today_response = self.client.get(
            "/api/loans/dashboard/analytics/",
            {"date_from": str(today), "date_to": str(today)},
        )
        self.assertEqual(today_response.status_code, 200, today_response.data)
        totals = today_response.data["totals"]
        self.assertEqual(totals["received_applications_count"], 1)
        self.assertEqual(totals["received_arrive_count"], 1)
        self.assertEqual(totals["received_organic_count"], 0)
        self.assertEqual(totals["approved_loans_count"], 1)
        self.assertEqual(totals["funded_loans_count"], 1)

        arrive_only = self.client.get(
            "/api/loans/dashboard/analytics/",
            {"date_from": str(today), "date_to": str(today), "source": "arrive"},
        )
        self.assertEqual(arrive_only.status_code, 200, arrive_only.data)
        self.assertEqual(arrive_only.data["totals"]["received_applications_count"], 1)
        self.assertEqual(arrive_only.data["totals"]["received_arrive_count"], 1)

        week_response = self.client.get(
            "/api/loans/dashboard/analytics/",
            {"date_from": str(yesterday), "date_to": str(today)},
        )
        self.assertEqual(week_response.status_code, 200, week_response.data)
        week_totals = week_response.data["totals"]
        self.assertEqual(week_totals["received_applications_count"], 2)
        self.assertEqual(week_totals["received_arrive_count"], 1)
        self.assertEqual(week_totals["received_organic_count"], 1)
        self.assertGreaterEqual(week_totals["approved_loans_count"], 2)

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

    def test_staff_can_approve_pending_signature_without_contract_signature(self):
        self.loan.status = "pending_signature"
        self.loan.contract_signed_at = None
        self.loan.save(update_fields=["status", "contract_signed_at", "updated_at"])

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(f"/api/loans/{self.loan.id}/approve/", {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertIsNone(self.loan.contract_signed_at)
        self.assertTrue(
            self.loan.state_events.filter(
                event_type="human_approved",
                previous_status="pending_signature",
                new_status="pending_funding",
            ).exists()
        )

    def test_customer_can_sign_contract_after_manual_approval(self):
        self.customer.banking_verified = True
        self.customer.contract_completed = False
        self.customer.onboarding_stage = "contract"
        self.customer.save(
            update_fields=[
                "banking_verified",
                "contract_completed",
                "onboarding_stage",
                "updated_at",
            ]
        )
        self.loan.status = "pending_funding"
        self.loan.approved_at = timezone.now()
        self.loan.contract_signed_at = None
        self.loan.save(update_fields=["status", "approved_at", "contract_signed_at", "updated_at"])
        Contract.objects.create(
            customer=self.customer,
            loan=self.loan,
            agreement_text="older duplicate draft",
        )
        Contract.objects.create(
            customer=self.customer,
            loan=self.loan,
            agreement_text="newer duplicate draft",
        )

        self.client.force_authenticate(user=self.customer_user)

        dashboard_response = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard_response.status_code, 200, dashboard_response.data)
        self.assertEqual(dashboard_response.data["portal_state"], "contract_required")
        self.assertEqual(dashboard_response.data["next_url"], "/customer/contracts")

        preview_response = self.client.get("/api/portal/me/contract-preview/")
        self.assertEqual(preview_response.status_code, 200, preview_response.data)
        self.assertEqual(str(preview_response.data["loan"]), str(self.loan.id))
        self.assertIn("APPLICATION CHANNEL:</strong> Landing", preview_response.data["agreement_text"])
        self.assertIn("Bank Account Funding", preview_response.data["agreement_text"])
        self.assertNotIn("Secured Card Funding", preview_response.data["agreement_text"])

        sign_response = self.client.post(
            "/api/portal/me/sign-contract/",
            {
                "typed_name": self.customer.full_name,
                "accepted_terms": True,
                "accepted_credit_check": True,
                "accepted_banking_review": True,
                "accepted_electronic_signature": True,
            },
            format="json",
        )
        self.assertEqual(sign_response.status_code, 200, sign_response.data)

        self.loan.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertIsNotNone(self.loan.contract_signed_at)
        self.assertTrue(self.customer.contract_completed)

    @patch("communications.tasks.send_email.delay")
    @patch("communications.tasks.send_template_message.delay")
    def test_loan_workflow_reminders_send_ibv_and_signature_once_per_day(
        self,
        send_template_delay,
        _send_email_delay,
    ):
        from communications.tasks import send_template_message

        send_template_delay.side_effect = send_template_message
        CommunicationTemplate.objects.create(
            name="IBV Reminder Template",
            type="email",
            subject="IBV reminder",
            content="Complete IBV at {{portal_url}}",
            is_active=True,
        )
        CommunicationTemplate.objects.create(
            name="Contract Signature Reminder Template",
            type="email",
            subject="Signature reminder",
            content="Sign at {{portal_url}}",
            is_active=True,
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDERS_ENABLED",
            defaults={"value": "True"},
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDER_MAX_DAYS",
            defaults={"value": "3"},
        )

        self.loan.status = "ibv_pending"
        self.loan.save(update_fields=["status", "updated_at"])

        approved_unsigned = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("300.00"),
            fee=Decimal("60.00"),
            total_amount=Decimal("360.00"),
            balance=Decimal("360.00"),
            status="pending_funding",
            approved_at=timezone.now(),
            is_active=True,
        )

        first_result = send_loan_workflow_reminders()
        second_result = send_loan_workflow_reminders()

        self.assertEqual(first_result, {"ibv_sent": 1, "ibv_expired": 0, "signature_sent": 1, "signature_expired": 0})
        self.assertEqual(second_result, {"ibv_sent": 0, "ibv_expired": 0, "signature_sent": 0, "signature_expired": 0})
        self.assertEqual(
            self.loan.communications.filter(template_name="IBV Reminder Template").count(),
            1,
        )
        self.assertEqual(
            approved_unsigned.communications.filter(
                template_name="Contract Signature Reminder Template"
            ).count(),
            1,
        )

    @patch("communications.tasks.send_email.delay")
    @patch("communications.tasks.send_template_message.delay")
    def test_loan_workflow_reminders_stop_after_max_days(
        self,
        send_template_delay,
        _send_email_delay,
    ):
        from communications.tasks import send_template_message

        send_template_delay.side_effect = send_template_message
        CommunicationTemplate.objects.create(
            name="IBV Reminder Template",
            type="email",
            subject="IBV reminder",
            content="Complete IBV at {{portal_url}}",
            is_active=True,
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDERS_ENABLED",
            defaults={"value": "True"},
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDER_MAX_DAYS",
            defaults={"value": "1"},
        )
        self.loan.status = "ibv_pending"
        self.loan.save(update_fields=["status", "updated_at"])
        Communication.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="email",
            direction="outbound",
            to_address=self.customer.email,
            subject="IBV reminder",
            content="Existing reminder",
            status="sent",
            template_name="IBV Reminder Template",
        )

        result = send_loan_workflow_reminders()

        self.assertEqual(result["ibv_sent"], 0)
        self.assertEqual(result["ibv_expired"], 1)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "expired")

    @patch("communications.tasks.send_email.delay")
    @patch("communications.tasks.send_template_message.delay")
    @patch("accounts.arrive_integration.queue_decision_webhook")
    def test_loan_workflow_reminders_expire_ibv_after_three_reminders_and_email(
        self,
        queue_decision_webhook,
        send_template_delay,
        _send_email_delay,
    ):
        from communications.tasks import send_template_message

        send_template_delay.side_effect = send_template_message
        CommunicationTemplate.objects.create(
            name="Application Expired Template",
            type="email",
            subject="Expired",
            content="Application expired. Start again at {{portal_url}}",
            is_active=True,
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDERS_ENABLED",
            defaults={"value": "True"},
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDER_MAX_DAYS",
            defaults={"value": "3"},
        )
        self.loan.status = "ibv_pending"
        self.loan.save(update_fields=["status", "updated_at"])
        for index in range(3):
            Communication.objects.create(
                customer=self.customer,
                loan=self.loan,
                type="email",
                direction="outbound",
                to_address=self.customer.email,
                subject=f"IBV reminder {index + 1}",
                content="Existing reminder",
                status="sent",
                template_name="IBV Reminder Template",
            )

        result = send_loan_workflow_reminders()

        self.loan.refresh_from_db()
        self.assertEqual(result, {"ibv_sent": 0, "ibv_expired": 1, "signature_sent": 0, "signature_expired": 0})
        self.assertEqual(self.loan.status, "expired")
        self.assertFalse(self.loan.is_active)
        queue_decision_webhook.assert_called_once_with(self.loan, "declined")
        send_template_delay.assert_called_once()
        self.assertEqual(
            self.loan.communications.filter(template_name="Application Expired Template").count(),
            1,
        )

    def test_signature_reminders_auto_expire_missing_contract(self):
        CommunicationTemplate.objects.create(
            name="Contract Signature Reminder Template",
            type="email",
            subject="Signature reminder",
            content="Sign at {{portal_url}}",
            is_active=True,
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDERS_ENABLED",
            defaults={"value": "True"},
        )
        GlobalSetting.objects.update_or_create(
            key="LOAN_WORKFLOW_REMINDER_MAX_DAYS",
            defaults={"value": "3"},
        )
        self.loan.status = "pending_funding"
        self.loan.approved_at = timezone.now()
        self.loan.contract_signed_at = None
        self.loan.save(update_fields=["status", "approved_at", "contract_signed_at", "updated_at"])
        for index in range(3):
            Communication.objects.create(
                customer=self.customer,
                loan=self.loan,
                type="email",
                direction="outbound",
                to_address=self.customer.email,
                subject=f"Signature reminder {index + 1}",
                content="Existing reminder",
                status="sent",
                template_name="Contract Signature Reminder Template",
            )

        result = send_loan_workflow_reminders()

        self.loan.refresh_from_db()
        self.assertEqual(result["signature_sent"], 0)
        self.assertEqual(result["signature_expired"], 1)
        self.assertEqual(result["ibv_expired"], 0)
        self.assertEqual(self.loan.status, "expired")
        self.assertFalse(self.loan.is_active)
        self.assertEqual(self.loan.decline_reason, "expired")


class StartNewApplicationTests(APITestCase):
    """Terminal declined/expired customers can open a second loan."""

    def setUp(self):
        self.portal_user = User.objects.create_user(
            email="declined@example.com",
            password="Password123!",
            full_name="Declined Customer",
            user_type="customer",
            phone="4165550199",
            phone_normalized="+14165550199",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Declined",
            last_name="Customer",
            email="declined@example.com",
            phone="4165550199",
            phone_normalized="+14165550199",
            province="ON",
            status="active",
            onboarding_stage="portal_active",
            banking_verified=True,
            contract_completed=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.declined_loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="human_declined",
            is_active=False,
            declined_at=timezone.now(),
            decline_reason="Unacceptable bank",
        )
        self.client.force_authenticate(self.portal_user)

    def test_dashboard_exposes_start_new_application_when_declined(self):
        response = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["portal_state"], "declined")
        self.assertTrue(response.data["can_start_new_application"])
        self.assertEqual(response.data["current_application"]["id"], str(self.declined_loan.id))

    def test_start_new_application_keeps_declined_loan_and_resets_onboarding(self):
        from banking.models import BankConnection

        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="old-login",
            sync_status="synced",
            is_active=True,
        )

        response = self.client.post("/api/portal/me/start-new-application/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["next_url"], "/customer/banking")
        self.assertFalse(response.data["can_start_new_application"])

        self.customer.refresh_from_db()
        self.declined_loan.refresh_from_db()
        connection.refresh_from_db()

        self.assertEqual(self.declined_loan.status, "human_declined")
        self.assertFalse(self.customer.banking_verified)
        self.assertFalse(self.customer.contract_completed)
        self.assertEqual(self.customer.onboarding_stage, "banking_verification")
        self.assertFalse(connection.is_active)

        new_loan = Loan.objects.get(id=response.data["loan_id"])
        self.assertEqual(new_loan.status, "ibv_pending")
        self.assertNotEqual(new_loan.id, self.declined_loan.id)
        self.assertEqual(self.customer.loans.count(), 2)

        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertEqual(dashboard.data["portal_state"], "awaiting_banking")
        self.assertEqual(
            dashboard.data["current_application"]["id"],
            str(new_loan.id),
        )
        self.assertEqual(
            dashboard.data["current_application"]["status"],
            "ibv_pending",
        )

    def test_start_new_application_blocked_when_active_loan_exists(self):
        Loan.objects.create(
            customer=self.customer,
            principal=Decimal("400.00"),
            fee=Decimal("80.00"),
            total_amount=Decimal("480.00"),
            balance=Decimal("480.00"),
            status="active",
            is_active=True,
        )

        response = self.client.post("/api/portal/me/start-new-application/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("in progress", response.data["error"].lower())

        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.data["portal_state"], "active_loan")
        self.assertFalse(dashboard.data["can_start_new_application"])

    def test_non_declined_customer_cannot_start_new_application(self):
        self.declined_loan.status = "paid_off"
        self.declined_loan.is_active = False
        self.declined_loan.declined_at = None
        self.declined_loan.decline_reason = None
        self.declined_loan.save()

        response = self.client.post("/api/portal/me/start-new-application/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("declined", response.data["error"].lower())
        self.assertFalse(
            self.client.get("/api/portal/me/dashboard/").data["can_start_new_application"]
        )

    def test_expired_customer_can_start_new_application(self):
        self.declined_loan.status = "expired"
        self.declined_loan.is_active = False
        self.declined_loan.declined_at = None
        self.declined_loan.decline_reason = None
        self.declined_loan.save()

        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertEqual(dashboard.data["portal_state"], "expired")
        self.assertTrue(dashboard.data["can_start_new_application"])

        response = self.client.post("/api/portal/me/start-new-application/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        new_loan = Loan.objects.get(id=response.data["loan_id"])
        self.assertEqual(new_loan.status, "ibv_pending")
        self.assertEqual(self.customer.loans.count(), 2)

    def test_start_new_application_preserves_arrive_customer_source(self):
        self.customer.source = Customer.SOURCE_ARRIVE
        self.customer.arrive_application_id = "arrive-app-123"
        self.customer.arrive_zum_user_id = "zum-user-123"
        self.customer.arrive_zum_user_card_id = "card-123"
        self.customer.arrive_event_id = "event-123"
        self.customer.save(
            update_fields=[
                "source",
                "arrive_application_id",
                "arrive_zum_user_id",
                "arrive_zum_user_card_id",
                "arrive_event_id",
                "updated_at",
            ]
        )

        response = self.client.post("/api/portal/me/start-new-application/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        self.customer.refresh_from_db()
        new_loan = Loan.objects.get(id=response.data["loan_id"])
        self.assertEqual(new_loan.customer, self.customer)
        self.assertEqual(self.customer.source, Customer.SOURCE_ARRIVE)
        self.assertEqual(self.customer.arrive_application_id, "arrive-app-123")
        self.assertEqual(self.customer.arrive_zum_user_id, "zum-user-123")
        self.assertEqual(self.customer.arrive_zum_user_card_id, "card-123")
        self.assertEqual(self.customer.arrive_event_id, "event-123")
