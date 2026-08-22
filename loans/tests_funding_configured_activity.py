from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from activity.models import ActivityHistory
from banking.models import BankAccount, BankConnection
from loans.models import Loan
from loans.zumrails import FundingConfigurationService


class FundingConfiguredActorTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="mo.test@example.com",
            password="password123",
            full_name="Mo Test",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.other_staff = User.objects.create_user(
            email="other.agent@example.com",
            password="password123",
            full_name="Other Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Pat",
            last_name="River",
            email="pat.river@example.com",
            phone="4165550199",
            province="ON",
            status="active",
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="login-funding-actor",
            sync_status="synced",
            is_active=True,
        )
        self.account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-funding-actor",
            name="CHQING 1",
            type="checking",
            transit_number="00016",
            institution_number="608",
            account_number="11117840",
            is_primary=True,
        )
        self.other_account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="acct-funding-actor-2",
            name="CHQING 2",
            type="checking",
            transit_number="00017",
            institution_number="608",
            account_number="22227840",
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
        self.client.force_authenticate(user=self.staff)

    def test_funding_options_auto_default_is_system_not_page_viewer(self):
        response = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")

        self.assertEqual(response.status_code, 200, response.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.bank_account_id, self.account.id)

        log = ActivityHistory.objects.get(loan=self.loan, title="Funding Configured")
        self.assertEqual(log.created_by, "system")
        self.assertEqual(log.metadata.get("actor"), "System")
        self.assertEqual(log.metadata.get("source"), "auto_default")
        self.assertIn("by System", log.description)
        self.assertNotIn("Mo Test", log.description)
        self.assertIn("applied automatically", log.description)

    def test_ensure_defaults_ignores_passed_staff_user(self):
        FundingConfigurationService.ensure_defaults(self.loan, user=self.staff)

        log = ActivityHistory.objects.get(loan=self.loan, title="Funding Configured")
        self.assertEqual(log.created_by, "system")
        self.assertNotIn("Mo Test", log.description)

    def test_explicit_staff_configuration_keeps_staff_name(self):
        FundingConfigurationService.ensure_defaults(self.loan)
        ActivityHistory.objects.filter(loan=self.loan, title="Funding Configured").delete()

        response = self.client.patch(
            f"/api/loans/{self.loan.id}/funding/configuration/",
            {
                "eft_bank_account_id": str(self.other_account.id),
                "collections_account_id": str(self.other_account.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        log = ActivityHistory.objects.filter(
            loan=self.loan, title="Funding Configured"
        ).latest("created_at")
        self.assertEqual(log.created_by, str(self.staff.id))
        self.assertEqual(log.metadata.get("source"), "staff")
        self.assertIn("by Mo Test", log.description)
        self.assertNotIn("applied automatically", log.description)
