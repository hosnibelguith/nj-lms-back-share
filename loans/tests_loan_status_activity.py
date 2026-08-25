from datetime import date, datetime, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from activity.models import ActivityHistory
from loans.models import Loan, Payment
from loans.services import LoanService


class LoanStatusActivityActorTests(APITestCase):
    def setUp(self):
        self.approver = User.objects.create_user(
            email="arki@example.com",
            password="password123",
            full_name="Arki",
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
            email="pat.river.status@example.com",
            phone="4165550188",
            province="ON",
            status="active",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="active",
            is_active=True,
            approved_by=self.approver,
            approved_at=timezone.now() - timedelta(days=20),
            funded_at=timezone.make_aware(datetime(2026, 8, 8, 12, 0)),
        )

    def _collections_row(self):
        return ActivityHistory.objects.filter(
            loan=self.loan, title="Loan In Collections"
        ).latest("created_at")

    def test_auto_collections_is_system_not_original_approver(self):
        self.loan.mark_defaulted(notes="In collections: missed payment")

        row = self._collections_row()
        self.assertEqual(row.created_by, "system")
        self.assertEqual(row.metadata.get("actor"), "System")
        self.assertIn("by System", row.description)
        self.assertNotIn("Arki", row.description)

        self.client.force_authenticate(user=self.other_staff)
        timeline = self.client.get(
            f"/api/activities/timeline/?customer_id={self.customer.id}"
        )
        self.assertEqual(timeline.status_code, 200, timeline.data)
        collections = next(
            item for item in timeline.data if item["title"] == "Loan In Collections"
        )
        self.assertEqual(collections["created_by_name"], "System")
        self.assertEqual(collections["metadata"]["actor"], "System")

    def test_staff_stop_uses_acting_agent_not_approver(self):
        self.loan.mark_stopped(user=self.other_staff, notes="Staff stopped collections")

        row = ActivityHistory.objects.filter(
            loan=self.loan, title="Loan Stopped"
        ).latest("created_at")
        self.assertEqual(row.created_by, str(self.other_staff.id))
        self.assertEqual(row.metadata.get("actor"), "Other Agent")
        self.assertIn("by Other Agent", row.description)
        self.assertNotIn("Arki", row.description)

    def test_reactivate_is_not_logged_as_loan_funded(self):
        self.loan.mark_stopped(notes="stopped")
        self.loan.reactivate(user=self.other_staff, notes="new agreement")

        self.assertFalse(
            ActivityHistory.objects.filter(loan=self.loan, title="Loan Funded").exists()
        )
        row = ActivityHistory.objects.filter(
            loan=self.loan, title="Loan Reactivated"
        ).latest("created_at")
        self.assertEqual(row.created_by, str(self.other_staff.id))
        self.assertIn("by Other Agent", row.description)
        self.assertIn("Stopped", row.description)
        self.assertIn("Active", row.description)

    def test_reactivate_keeps_nsf_fees_balance_and_original_funding_date(self):
        nsf_extra = LoanService.COLLECTION_FAILURE_FEE_AMOUNT
        original_funded_at = self.loan.funded_at
        self.loan.fee = Decimal("150.00")
        self.loan.total_amount = Decimal("650.00")
        self.loan.balance = Decimal("650.00")
        self.loan.save(update_fields=["fee", "total_amount", "balance", "updated_at"])
        missed = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("147.18"),
            scheduled_date=date(2026, 8, 15),
            status="nsf",
            type="scheduled",
            failure_reason="EftFailedAccountClosed",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=nsf_extra,
            scheduled_date=date(2026, 8, 29),
            status="scheduled",
            type="scheduled",
            notes=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("600.00"),
            scheduled_date=date(2026, 8, 22),
            status="scheduled",
            type="scheduled",
        )

        self.loan.mark_stopped(notes="Collections stopped: closed account")
        self.assertEqual(
            self.loan.payments.filter(status="unscheduled").count(),
            2,
        )

        self.client.force_authenticate(user=self.other_staff)
        response = self.client.post(
            f"/api/loans/{self.loan.id}/reactivate/",
            {
                "notes": "New void cheque on file",
                "start_date": str(timezone.localdate()),
                "frequency": "bi-weekly",
                "payment_amount": "50.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.loan.refresh_from_db()
        missed.refresh_from_db()
        self.assertEqual(self.loan.status, "active")
        self.assertEqual(self.loan.fee, Decimal("150.00"))
        self.assertEqual(self.loan.total_amount, Decimal("650.00"))
        self.assertEqual(self.loan.balance, Decimal("650.00"))
        self.assertEqual(self.loan.funded_at, original_funded_at)
        self.assertEqual(missed.status, "nsf")
        self.assertEqual(missed.amount, Decimal("147.18"))
        self.assertFalse(self.loan.payments.filter(status="unscheduled").exists())
        scheduled_total = sum(
            (row.amount for row in self.loan.payments.filter(status="scheduled")),
            Decimal("0.00"),
        )
        self.assertEqual(scheduled_total, Decimal("650.00"))
        self.assertFalse(
            ActivityHistory.objects.filter(loan=self.loan, title="Loan Funded").exists()
        )
        row = ActivityHistory.objects.filter(
            loan=self.loan, title="Loan Reactivated"
        ).latest("created_at")
        self.assertEqual(row.created_by, str(self.other_staff.id))
        self.assertIn("Reactivated by Other Agent", row.description)
        self.assertNotIn("Arki", row.description)
