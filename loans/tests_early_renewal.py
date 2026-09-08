from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, CustomerDocument, GlobalSetting, User
from communications.models import Communication, CommunicationTemplate
from loans.models import CollectionPayment, FundedPayment, Loan, LoanFormula, Payment
from loans.renewal import (
    EARLY_RENEWAL_TEMPLATE_NAME,
    SETTING_SHORT_MAX_REMAINING,
    early_renewal_ineligible_reason,
    early_renewal_offer,
    is_early_renewal_eligible,
)
from loans.services import LoanService
from loans.tasks import send_early_renewal_offers
from loans.zumrails import FundingService, apply_funded_payment_zum_status


class EarlyRenewalTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="renewal-staff@example.com",
            password="password123",
            full_name="Renewal Staff",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="renewal-customer@example.com",
            password="password123",
            full_name="Renewal Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Renewal",
            last_name="Customer",
            email="renewal-customer@example.com",
            phone="4165550202",
            province="ON",
            status="active",
            banking_verified=True,
            contract_completed=True,
            requested_loan_amount=Decimal("500.00"),
        )
        self.long_formula = LoanFormula.objects.create(
            name="Early Renewal Long 500",
            principal_amount=Decimal("500.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("29.00"),
            default_number_of_payments=6,
            default_frequency_days=14,
            is_active=True,
        )
        self.loan = self._active_loan(
            balance=Decimal("100.00"),
            remaining=2,
            formula=self.long_formula,
        )
        CustomerDocument.objects.create(
            customer=self.customer,
            document_type=CustomerDocument.TYPE_GOVERNMENT_ID,
            file=SimpleUploadedFile("id.pdf", b"%PDF-1.4 id", content_type="application/pdf"),
            original_filename="id.pdf",
            content_type="application/pdf",
        )
        self.client.force_authenticate(user=self.staff)

    def _active_loan(self, *, balance, remaining, principal=None, **extra):
        loan = Loan.objects.create(
            customer=self.customer,
            principal=principal or Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=balance,
            status="active",
            is_active=True,
            **extra,
        )
        payment_amount = LoanService.money(balance / remaining) if remaining else Decimal("0.00")
        for index in range(remaining):
            Payment.objects.create(
                loan=loan,
                amount=payment_amount,
                scheduled_date=timezone.localdate() + timedelta(days=14 * (index + 1)),
                status="scheduled",
            )
        return loan

    def test_short_loan_eligible_only_with_one_payment_left(self):
        short_two = self._active_loan(balance=Decimal("100.00"), remaining=2)
        self.assertFalse(is_early_renewal_eligible(short_two))
        self.assertIn("1 payment left", early_renewal_ineligible_reason(short_two))

        short_one = self._active_loan(balance=Decimal("80.00"), remaining=1)
        self.assertTrue(is_early_renewal_eligible(short_one))

    def test_long_or_larger_loan_eligible_with_one_or_two_remaining(self):
        self.assertTrue(is_early_renewal_eligible(self.loan))
        one_left = self._active_loan(
            balance=Decimal("80.00"),
            remaining=1,
            formula=self.long_formula,
        )
        self.assertTrue(is_early_renewal_eligible(one_left))

        by_amount = self._active_loan(
            balance=Decimal("200.00"),
            remaining=2,
            principal=Decimal("1000.00"),
        )
        self.assertTrue(is_early_renewal_eligible(by_amount))

        by_duration = self._active_loan(balance=Decimal("100.00"), remaining=2)
        payments = list(by_duration.payments.order_by("scheduled_date"))
        payments[1].scheduled_date = payments[0].scheduled_date + timedelta(days=50)
        payments[1].save(update_fields=["scheduled_date"])
        self.assertTrue(is_early_renewal_eligible(by_duration))

    def test_remaining_window_is_adjustable_via_settings(self):
        short_two = self._active_loan(balance=Decimal("100.00"), remaining=2)
        self.assertFalse(is_early_renewal_eligible(short_two))

        GlobalSetting.objects.create(key=SETTING_SHORT_MAX_REMAINING, value="2")
        self.assertTrue(is_early_renewal_eligible(short_two))

    def test_ineligible_with_three_payments_pending_stopped_or_arrive(self):
        three = self._active_loan(
            balance=Decimal("300.00"),
            remaining=3,
            formula=self.long_formula,
        )
        self.assertFalse(is_early_renewal_eligible(three))

        pending = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate(),
            status="pending",
        )
        self.assertFalse(is_early_renewal_eligible(self.loan))
        pending.delete()

        self.loan.status = "stopped"
        self.loan.save(update_fields=["status", "updated_at"])
        self.assertFalse(is_early_renewal_eligible(self.loan))
        self.loan.status = "active"
        self.loan.save(update_fields=["status", "updated_at"])

        self.customer.source = Customer.SOURCE_ARRIVE
        self.customer.save(update_fields=["source", "updated_at"])
        self.assertIsNone(LoanService.early_renewal_offer_for_customer(self.customer))

    def test_dirty_payment_history_blocks_automatic_eligibility(self):
        nsf = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate() - timedelta(days=14),
            status="nsf",
        )
        self.assertFalse(is_early_renewal_eligible(self.loan))
        nsf.delete()
        self.assertTrue(is_early_renewal_eligible(self.loan))

        failed = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=timezone.localdate() - timedelta(days=7),
            status="failed",
            failure_reason="Payment failed",
        )
        self.assertFalse(is_early_renewal_eligible(self.loan))
        failed.delete()

        returned = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="returned",
            failure_reason="NSF",
        )
        self.assertFalse(is_early_renewal_eligible(self.loan))
        returned.delete()

        stop_pay = CollectionPayment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            status="failed",
            failure_reason="EftFailedStopPayment",
        )
        self.assertFalse(is_early_renewal_eligible(self.loan))
        stop_pay.delete()
        self.assertTrue(is_early_renewal_eligible(self.loan))

    def test_offer_shows_new_amount_remaining_deducted_and_net(self):
        offer = early_renewal_offer(self.loan)
        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["new_loan_amount"], "500.00")
        self.assertEqual(offer["old_balance"], "100.00")
        self.assertEqual(offer["remaining_balance"], "100.00")
        self.assertEqual(offer["amount_deducted"], "100.00")
        self.assertEqual(offer["net_to_client"], "400.00")
        self.assertEqual(offer["window"], "long")
        self.assertEqual(offer["max_remaining"], 2)

    def test_start_early_renewal_keeps_old_loan_and_ibv(self):
        self.customer.source = Customer.SOURCE_ORGANIC
        self.customer.banking_verified = True
        self.customer.save(update_fields=["source", "banking_verified", "updated_at"])

        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)
        self.loan.refresh_from_db()
        self.customer.refresh_from_db()

        self.assertEqual(self.loan.status, "active")
        self.assertEqual(new_loan.previous_loan_id, self.loan.id)
        self.assertEqual(new_loan.principal, Decimal("500.00"))
        self.assertEqual(new_loan.status, "pending_signature")
        self.assertTrue(self.customer.banking_verified)
        self.assertFalse(self.customer.contract_completed)
        self.assertFalse(new_loan.contract_signed)

        blocked = self.client.post(
            f"/api/loans/{self.loan.id}/start-early-renewal/",
            {},
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)

    def test_create_initial_application_still_blocked_by_active_loan(self):
        existing = LoanService.create_initial_application(self.customer)
        self.assertEqual(existing.id, self.loan.id)

    def test_funding_sends_net_and_closes_old_loan(self):
        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)
        new_loan.status = "pending_funding"
        new_loan.contract_signed_at = timezone.now()
        new_loan.save(update_fields=["status", "contract_signed_at", "updated_at"])
        self.customer.contract_completed = True
        self.customer.save(update_fields=["contract_completed", "updated_at"])

        funded = LoanService.fund_loan(new_loan, method="eft", user=self.staff)
        self.loan.refresh_from_db()
        funded.refresh_from_db()
        payment = FundedPayment.objects.get(loan=funded)

        self.assertEqual(payment.amount, Decimal("400.00"))
        self.assertEqual(funded.status, "active")
        self.assertEqual(funded.principal, Decimal("500.00"))
        self.assertEqual(funded.renewal_payoff_amount, Decimal("100.00"))
        self.assertEqual(self.loan.status, "paid_off")
        self.assertEqual(self.loan.balance, Decimal("0.00"))
        self.assertFalse(self.loan.payments.filter(status="scheduled").exists())
        self.assertTrue(
            self.loan.payments.filter(
                type="manual",
                status="completed",
                amount=Decimal("100.00"),
            ).exists()
        )

        LoanService.finalize_early_renewal_funding(funded, user=self.staff)
        self.assertEqual(
            self.loan.payments.filter(type="manual", status="completed").count(),
            1,
        )

    @patch("loans.zumrails.ZumRailsService.initiate_transaction", return_value="zum-renew-1")
    def test_processor_funding_uses_net_disbursement(self, _mock_send):
        from banking.models import BankAccount, BankConnection

        connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="renew-login",
            sync_status="synced",
        )
        account = BankAccount.objects.create(
            connection=connection,
            customer=self.customer,
            external_id="renew-acct",
            name="Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)
        new_loan.status = "pending_funding"
        new_loan.contract_signed_at = timezone.now()
        new_loan.bank_account = account
        new_loan.collections_account = account
        new_loan.funding_destination = {"method": "eft", "account": {"id": str(account.id)}}
        new_loan.save()
        self.customer.contract_completed = True
        self.customer.save(update_fields=["contract_completed", "updated_at"])

        funding = FundingService.initiate(
            new_loan,
            method="eft",
            schedule_confirmed=True,
            user=self.staff,
            destination={"bank_account_id": str(account.id)},
            collections_account=account,
        )
        self.loan.refresh_from_db()
        new_loan.refresh_from_db()
        self.assertEqual(funding.amount, Decimal("400.00"))
        self.assertEqual(new_loan.status, "active")
        self.assertEqual(self.loan.status, "paid_off")

    def test_webhook_completed_does_not_double_payoff(self):
        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)
        new_loan.status = "pending_funding"
        new_loan.contract_signed_at = timezone.now()
        new_loan.save(update_fields=["status", "contract_signed_at", "updated_at"])
        self.customer.contract_completed = True
        self.customer.save(update_fields=["contract_completed", "updated_at"])
        LoanService.fund_loan(new_loan, method="eft", user=self.staff)
        funding = FundedPayment.objects.get(loan=new_loan)
        apply_funded_payment_zum_status(funding, status_value="Completed")
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "paid_off")
        self.assertEqual(
            self.loan.payments.filter(type="manual", status="completed").count(),
            1,
        )

    def test_portal_and_staff_start_endpoints(self):
        self.client.force_authenticate(user=self.portal_user)
        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertTrue(dashboard.data["can_renew"])
        offer = dashboard.data["early_renewal"]
        self.assertEqual(offer["new_loan_amount"], "500.00")
        self.assertEqual(offer["old_balance"], "100.00")
        self.assertEqual(offer["remaining_balance"], "100.00")
        self.assertEqual(offer["amount_deducted"], "100.00")
        self.assertEqual(offer["net_to_client"], "400.00")

        started = self.client.post("/api/portal/me/start-early-renewal/", {}, format="json")
        self.assertEqual(started.status_code, 200, started.data)
        new_loan = Loan.objects.get(pk=started.data["loan_id"])
        self.assertEqual(new_loan.previous_loan_id, self.loan.id)

        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertFalse(dashboard.data["can_renew"])

    def test_email_offer_sends_once(self):
        template = CommunicationTemplate.objects.filter(
            name=EARLY_RENEWAL_TEMPLATE_NAME,
            type="email",
            is_active=True,
        ).first()
        if template is None:
            template = CommunicationTemplate.objects.create(
                name=EARLY_RENEWAL_TEMPLATE_NAME,
                type="email",
                trigger="manual",
                subject="You qualify for an early renewal",
                content="Net {{net_to_client}} after deducting {{amount_deducted}}.",
                is_active=True,
            )
        with patch("communications.tasks.send_template_message.delay") as send_delay:
            first = send_early_renewal_offers()
            Communication.objects.create(
                customer=self.customer,
                loan=self.loan,
                type="email",
                direction="outbound",
                to_address=self.customer.email,
                subject="You qualify for an early renewal",
                content="sent",
                status="sent",
                template_name=EARLY_RENEWAL_TEMPLATE_NAME,
            )
            second = send_early_renewal_offers()

        send_delay.assert_called_once()
        kwargs = send_delay.call_args.kwargs
        self.assertEqual(kwargs["extra_context"]["new_loan_amount"], "500.00")
        self.assertEqual(kwargs["extra_context"]["old_balance"], "100.00")
        self.assertEqual(kwargs["extra_context"]["remaining_balance"], "100.00")
        self.assertEqual(kwargs["extra_context"]["amount_deducted"], "100.00")
        self.assertEqual(kwargs["extra_context"]["net_to_client"], "400.00")
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(str(send_delay.call_args.args[1]), str(template.id))

    def test_staff_can_update_approved_amount_on_early_renewal_application(self):
        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)
        LoanFormula.objects.create(
            name="Early Renewal 700",
            principal_amount=Decimal("700.00"),
            brokerage_percent=Decimal("70.00"),
            repayment_percent=Decimal("29.00"),
            default_number_of_payments=6,
            default_frequency_days=14,
            is_active=True,
            is_default=True,
        )

        response = self.client.patch(
            f"/api/loans/{new_loan.id}/approved-amount/",
            {"principal": "700", "notes": ""},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        new_loan.refresh_from_db()
        self.loan.refresh_from_db()
        self.assertEqual(new_loan.principal, Decimal("700.00"))
        self.assertEqual(self.loan.status, "active")
        self.assertGreater(new_loan.principal, self.loan.balance)
        self.assertTrue(
            new_loan.state_events.filter(event_type="amount_updated").exists()
        )

    def test_approved_amount_rejected_when_below_old_renewal_balance(self):
        new_loan = LoanService.start_early_renewal(self.customer, user=self.staff)

        response = self.client.patch(
            f"/api/loans/{new_loan.id}/approved-amount/",
            {"principal": "50.00", "notes": ""},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("old loan balance", response.data["error"])
        new_loan.refresh_from_db()
        self.assertEqual(new_loan.principal, Decimal("500.00"))
