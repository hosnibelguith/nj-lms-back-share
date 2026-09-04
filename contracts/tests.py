from decimal import Decimal

from django.test import TestCase

from accounts.models import Customer
from contracts.models import Contract
from contracts.services import (
    AGREEMENT_VERSION,
    build_agreement_context,
    is_arrive_customer,
    render_loan_agreement,
)
from loans.models import Loan


class LoanAgreementChannelTests(TestCase):
    def _customer(self, **overrides):
        data = {
            "first_name": "Alex",
            "last_name": "Casault",
            "email": "landing.customer@example.com",
            "phone": "5145550100",
            "province": "QC",
            "status": "active",
            "source": Customer.SOURCE_ORGANIC,
        }
        data.update(overrides)
        return Customer.objects.create(**data)

    def _loan(self, customer, **overrides):
        data = {
            "customer": customer,
            "principal": Decimal("300.00"),
            "fee": Decimal("150.00"),
            "total_amount": Decimal("450.00"),
            "balance": Decimal("450.00"),
            "status": "pending_signature",
            "is_active": True,
        }
        data.update(overrides)
        return Loan.objects.create(**data)

    def test_landing_and_arrive_share_one_template_with_channel_copy(self):
        landing_customer = self._customer()
        arrive_customer = self._customer(
            email="arrive.customer@example.com",
            source=Customer.SOURCE_ARRIVE,
            arrive_application_id="arrive-app-1",
        )
        landing_loan = self._loan(landing_customer)
        arrive_loan = self._loan(arrive_customer)

        landing = render_loan_agreement(landing_customer, landing_loan)
        arrive = render_loan_agreement(arrive_customer, arrive_loan)

        self.assertFalse(is_arrive_customer(landing_customer))
        self.assertTrue(is_arrive_customer(arrive_customer))
        self.assertEqual(
            build_agreement_context(landing_customer, landing_loan)["application_channel"],
            "Landing",
        )
        self.assertEqual(
            build_agreement_context(arrive_customer, arrive_loan)["application_channel"],
            "Arrive",
        )

        self.assertIn("APPLICATION CHANNEL:</strong> Landing", landing)
        self.assertIn("Bank Account Funding Authorization", landing)
        self.assertIn("Bank Account Funding", landing)
        self.assertIn("electronic funds transfer (EFT)", landing)
        self.assertIn("Interac e-Transfer", landing)
        self.assertNotIn("APPLICATION CHANNEL:</strong> Arrive", landing)
        self.assertNotIn("Secured Card Funding", landing)
        self.assertNotIn("secured card account", landing)

        self.assertIn("APPLICATION CHANNEL:</strong> Arrive", arrive)
        self.assertIn("Secured Card Funding Authorization", arrive)
        self.assertIn("Secured Card Funding", arrive)
        self.assertIn("secured card account", arrive)
        self.assertNotIn("APPLICATION CHANNEL:</strong> Landing", arrive)
        self.assertNotIn("Bank Account Funding Authorization", arrive)
        self.assertNotIn("Interac e-Transfer", arrive)

        self.assertIn("MohawkLoans", landing)
        self.assertIn("MohawkLoans", arrive)
        self.assertIn("PRE-AUTHORIZED DEBIT (PAD) AGREEMENT", landing)
        self.assertIn("PRE-AUTHORIZED DEBIT (PAD) AGREEMENT", arrive)
        self.assertEqual(AGREEMENT_VERSION, "mohawk-channel-v2")
        self.assertEqual(Contract.AGREEMENT_VERSION, AGREEMENT_VERSION)

    def test_arrive_application_id_uses_arrive_copy_even_if_source_is_organic(self):
        customer = self._customer(
            email="linked.customer@example.com",
            source=Customer.SOURCE_ORGANIC,
            arrive_application_id="arrive-app-organic-source",
        )
        loan = self._loan(customer)
        html = render_loan_agreement(customer, loan)

        self.assertTrue(is_arrive_customer(customer))
        self.assertIn("APPLICATION CHANNEL:</strong> Arrive", html)
        self.assertIn("Secured Card Funding", html)
        self.assertNotIn("APPLICATION CHANNEL:</strong> Landing", html)

    def test_new_draft_contract_uses_channel_version(self):
        customer = self._customer(email="draft.customer@example.com")
        loan = self._loan(customer)
        contract = Contract.objects.create(
            customer=customer,
            loan=loan,
            agreement_text=render_loan_agreement(customer, loan),
        )
        self.assertEqual(contract.agreement_version, AGREEMENT_VERSION)
        self.assertIn("Landing", contract.agreement_text)
        self.assertIn("Bank Account Funding", contract.agreement_text)
