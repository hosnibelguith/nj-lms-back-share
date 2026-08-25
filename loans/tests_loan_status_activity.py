from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from activity.models import ActivityHistory
from loans.models import Loan


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
            approved_at=timezone.now(),
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
