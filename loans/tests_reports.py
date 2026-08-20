from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from activity.models import Comment
from communications.models import Communication
from loans.models import Loan, Payment
from loans.reports import list_report_types
from loans.services import LoanService


class StaffReportCenterTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="reports-agent@example.com",
            password="password123",
            full_name="Reports Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Riley",
            last_name="Cole",
            email="riley.cole@example.com",
            phone="4165552222",
            province="ON",
            status="active",
            source="organic",
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="active",
            is_active=True,
        )
        self.inside = timezone.make_aware(datetime(2026, 8, 10, 12, 0))
        self.client.force_authenticate(user=self.staff)

    def test_report_types_lists_lms_reports_and_omits_unrelated_products(self):
        response = self.client.get("/api/loans/report-types/")

        self.assertEqual(response.status_code, 200, response.data)
        ids = [item["id"] for item in response.data["results"]]
        self.assertIn("loan", ids)
        self.assertIn("payment", ids)
        self.assertIn("customer", ids)
        self.assertIn("fees", ids)
        self.assertNotIn("nacha", ids)
        self.assertNotIn("cash_drawer", ids)
        self.assertNotIn("vehicle", ids)
        self.assertNotIn("gamification", ids)
        self.assertEqual(ids, [item["id"] for item in list_report_types()])

    def test_unknown_report_type_is_rejected(self):
        response = self.client.get("/api/loans/reports/", {"report_type": "nacha"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Unknown", response.data["error"])

    def test_customer_and_loan_reports_return_live_rows(self):
        customers = self.client.get("/api/loans/reports/", {"report_type": "customer"})
        loans = self.client.get("/api/loans/reports/", {"report_type": "loan"})

        self.assertEqual(customers.status_code, 200, customers.data)
        self.assertEqual(loans.status_code, 200, loans.data)
        self.assertEqual(customers.data["count"], 1)
        self.assertEqual(customers.data["results"][0]["email"], "riley.cole@example.com")
        self.assertEqual(loans.data["results"][0]["customer_name"], "Riley Cole")
        self.assertEqual(Decimal(loans.data["results"][0]["principal"]), Decimal("500.00"))

    def test_payment_report_honors_scheduled_date_range(self):
        matching = Payment.objects.create(
            loan=self.loan,
            amount=Decimal("176.61"),
            scheduled_date=date(2026, 8, 10),
            status="scheduled",
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("50.00"),
            scheduled_date=date(2026, 7, 1),
            status="scheduled",
        )

        response = self.client.get(
            "/api/loans/reports/",
            {
                "report_type": "payment",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["payment_id"], str(matching.id))
        self.assertEqual(Decimal(response.data["results"][0]["amount"]), Decimal("176.61"))

    def test_fees_report_includes_deferral_and_nsf_fee_rows_only(self):
        deferral = Payment.objects.create(
            loan=self.loan,
            amount=LoanService.DEFERRAL_FEE_AMOUNT,
            scheduled_date=date(2026, 8, 12),
            status="scheduled",
            notes=LoanService.DEFERRAL_FEE_NOTE,
        )
        nsf = Payment.objects.create(
            loan=self.loan,
            amount=LoanService.COLLECTION_FAILURE_FEE_AMOUNT,
            scheduled_date=date(2026, 8, 13),
            status="scheduled",
            notes=LoanService.COLLECTION_FAILURE_FEE_NOTE,
        )
        Payment.objects.create(
            loan=self.loan,
            amount=Decimal("176.61"),
            scheduled_date=date(2026, 8, 14),
            status="scheduled",
            notes="Regular installment",
        )

        response = self.client.get("/api/loans/reports/", {"report_type": "fees"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        fee_types = {row["fee_type"]: row["payment_id"] for row in response.data["results"]}
        self.assertEqual(fee_types["deferral"], str(deferral.id))
        self.assertEqual(fee_types["nsf"], str(nsf.id))

    def test_opt_in_and_notes_reports_use_customer_records(self):
        self.customer.sms_opted_out = True
        self.customer.sms_opted_out_at = self.inside
        self.customer.sms_opt_out_reason = "STOP"
        self.customer.save(
            update_fields=["sms_opted_out", "sms_opted_out_at", "sms_opt_out_reason"]
        )
        comment = Comment.objects.create(
            customer=self.customer,
            loan=self.loan,
            content="Called about NSF",
            created_by=self.staff,
        )

        opt_ins = self.client.get("/api/loans/reports/", {"report_type": "opt_in"})
        notes = self.client.get("/api/loans/reports/", {"report_type": "notes"})

        self.assertEqual(opt_ins.status_code, 200, opt_ins.data)
        self.assertEqual(notes.status_code, 200, notes.data)
        self.assertFalse(opt_ins.data["results"][0]["sms_opted_in"])
        self.assertEqual(notes.data["results"][0]["note_id"], str(comment.id))
        self.assertEqual(notes.data["results"][0]["content"], "Called about NSF")

    def test_communication_report_returns_outbound_messages(self):
        message = Communication.objects.create(
            customer=self.customer,
            loan=self.loan,
            type="sms",
            direction="outbound",
            status="sent",
            content="Payment reminder",
            to_phone="4165552222",
            template_name="payment_reminder",
        )

        response = self.client.get("/api/loans/reports/", {"report_type": "communication"})
        automation = self.client.get("/api/loans/reports/", {"report_type": "automation"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["results"][0]["communication_id"], str(message.id))
        self.assertEqual(automation.data["count"], 1)
        self.assertEqual(automation.data["results"][0]["template_name"], "payment_reminder")

    def test_unauthenticated_staff_reports_are_rejected(self):
        self.client.force_authenticate(user=None)
        response = self.client.get("/api/loans/reports/", {"report_type": "loan"})
        self.assertIn(response.status_code, (401, 403))
