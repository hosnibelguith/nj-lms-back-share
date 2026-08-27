from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from communications.models import Communication, CommunicationTemplate
from loans.models import FundedPayment, Loan
from loans.zumrails import FundingService, apply_funded_payment_zum_status


class FundingCompletedEmailTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="fund-email@example.com",
            password="password123",
            full_name="Fund Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Funded",
            last_name="Client",
            email="funded.client@example.com",
            phone="4165550144",
            province="ON",
            status="active",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending_funding",
            contract_signed_at=timezone.now(),
            is_active=True,
        )
        self.template = CommunicationTemplate.objects.filter(
            name="Fund/Approve Template",
            type="email",
            is_active=True,
        ).first()
        if self.template is None:
            self.template = CommunicationTemplate.objects.create(
                name="Fund/Approve Template",
                type="email",
                trigger="loan_funded",
                subject="Your loan has been funded",
                content="Funds are processing.",
                is_active=True,
            )

    def _processing_funding(self):
        return FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="processing",
            processor_transaction_id="funding-email-1",
        )

    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.zumrails.transaction.on_commit")
    def test_zum_completed_queues_fund_approve_email(self, on_commit, send_delay):
        on_commit.side_effect = lambda callback: callback()
        funding = self._processing_funding()

        apply_funded_payment_zum_status(funding, status_value="Completed")

        send_delay.assert_called_once_with(
            str(self.customer.id),
            str(self.template.id),
            str(self.loan.id),
        )

    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.zumrails.transaction.on_commit")
    def test_zum_completed_does_not_duplicate_existing_fund_email(
        self, on_commit, send_delay
    ):
        on_commit.side_effect = lambda callback: callback()
        Communication.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="email",
            direction="outbound",
            subject="Your loan has been funded",
            to_address=self.customer.email,
            content="Already sent",
            status="sent",
            template_name="Fund/Approve Template",
        )
        funding = self._processing_funding()

        apply_funded_payment_zum_status(funding, status_value="Completed")

        send_delay.assert_not_called()

    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.zumrails.transaction.on_commit")
    def test_completed_replay_does_not_queue_email_again(self, on_commit, send_delay):
        on_commit.side_effect = lambda callback: callback()
        funding = FundedPayment.objects.create(
            loan=self.loan,
            amount=self.loan.principal,
            method="eft",
            status="completed",
            processor_transaction_id="funding-email-replay",
            completed_at=timezone.now(),
        )
        self.loan.status = "active"
        self.loan.is_active = True
        self.loan.funded_at = timezone.now()
        self.loan.save(update_fields=["status", "is_active", "funded_at", "updated_at"])

        apply_funded_payment_zum_status(funding, status_value="Completed")

        send_delay.assert_not_called()

    @patch("communications.tasks.send_template_message.delay")
    @patch("loans.zumrails.transaction.on_commit")
    def test_queue_helper_is_idempotent_for_same_loan(self, on_commit, send_delay):
        on_commit.side_effect = lambda callback: callback()
        FundingService._queue_funding_email(self.loan)
        Communication.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="email",
            direction="outbound",
            subject="Your loan has been funded",
            to_address=self.customer.email,
            content="Sent",
            status="sent",
            template_name="Fund/Approve Template",
        )
        FundingService._queue_funding_email(self.loan)
        send_delay.assert_called_once()
