from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Customer, CustomerDocument, User
from banking.models import BankAccount, BankConnection
from loans.models import Loan
from loans.services import LoanService
from loans.zumrails import funding_configuration_ready


def add_government_id(customer):
    return CustomerDocument.objects.create(
        customer=customer,
        document_type=CustomerDocument.TYPE_GOVERNMENT_ID,
        file=SimpleUploadedFile("id.pdf", b"%PDF-1.4 id", content_type="application/pdf"),
        original_filename="id.pdf",
        content_type="application/pdf",
    )


class PendingIdWorkflowTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="id-staff@example.com",
            password="password123",
            full_name="ID Staff",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="id-customer@example.com",
            password="password123",
            full_name="ID Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="ID",
            last_name="Customer",
            email="id-customer@example.com",
            phone="4165550404",
            province="ON",
            status="pending",
            banking_verified=True,
            contract_completed=False,
            requested_loan_amount=Decimal("500.00"),
        )
        self.connection = BankConnection.objects.create(
            customer=self.customer,
            login_id="id-login",
            sync_status="synced",
        )
        self.account = BankAccount.objects.create(
            connection=self.connection,
            customer=self.customer,
            external_id="id-acct",
            name="Chequing",
            type="checking",
            transit_number="12345",
            institution_number="003",
            account_number="1234567890",
            is_primary=True,
        )
        self.loan = Loan.objects.create(
            customer=self.customer,
            principal=Decimal("500.00"),
            fee=Decimal("100.00"),
            total_amount=Decimal("600.00"),
            balance=Decimal("600.00"),
            status="pending_signature",
            is_active=True,
            bank_account=self.account,
            collections_account=self.account,
        )

    def test_signing_without_id_moves_to_pending_id(self):
        self.customer.banking_verified = True
        self.customer.save(update_fields=["banking_verified", "updated_at"])
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")
        self.assertTrue(self.loan.contract_signed)
        self.assertFalse(self.loan.has_government_id)

    def test_signing_with_id_already_on_file_goes_to_pending(self):
        add_government_id(self.customer)
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending")
        self.assertTrue(self.loan.has_government_id)

    def test_approve_signed_loan_without_id_stays_off_funding_queue(self):
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        LoanService.approve_loan(self.loan, approved_by=self.staff)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")
        self.assertIsNotNone(self.loan.approved_at)

    def test_approve_unsigned_loan_without_id_still_goes_to_pending_funding(self):
        LoanService.approve_loan(self.loan, approved_by=self.staff)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertFalse(self.loan.contract_signed)

    def test_portal_dashboard_asks_for_id_after_signature(self):
        LoanService.sign_customer_contract(self.customer)
        self.client.force_authenticate(user=self.portal_user)
        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertEqual(dashboard.data["portal_state"], "id_required")
        self.assertEqual(dashboard.data["next_url"], "/customer/loans")
        self.assertEqual(dashboard.data["current_application"]["status"], "pending_id")

    def test_portal_can_upload_government_id_and_advance_pending_id(self):
        LoanService.sign_customer_contract(self.customer)
        self.client.force_authenticate(user=self.portal_user)
        uploaded = self.client.post(
            "/api/portal/me/documents/",
            {
                "document_type": "government_id",
                "file": SimpleUploadedFile(
                    "license.pdf",
                    b"%PDF-1.4 license",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending")
        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.data["portal_state"], "manual_review")

    def test_staff_id_upload_advances_approved_pending_id_to_funding(self):
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        LoanService.approve_loan(self.loan, approved_by=self.staff)
        self.client.force_authenticate(user=self.staff)
        uploaded = self.client.post(
            f"/api/customers/{self.customer.id}/documents/",
            {
                "document_type": "government_id",
                "file": SimpleUploadedFile(
                    "id.pdf",
                    b"%PDF-1.4 id",
                    content_type="application/pdf",
                ),
            },
            format="multipart",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")

    def test_funding_blocked_without_government_id(self):
        add_government_id(self.customer)
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        LoanService.approve_loan(self.loan, approved_by=self.staff)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")

        self.customer.documents.filter(
            document_type=CustomerDocument.TYPE_GOVERNMENT_ID
        ).delete()
        self.loan.refresh_from_db()
        self.assertFalse(self.loan.has_government_id)

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/loans/{self.loan.id}/funding/initiate/",
            {"method": "eft", "schedule_confirmed": True, "override_confirmed": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["error"],
            "Government ID must be uploaded before funding.",
        )
        options = self.client.get(f"/api/loans/{self.loan.id}/funding/options/")
        self.assertEqual(options.status_code, 200)
        self.assertIn(
            "Government ID must be uploaded before funding.",
            options.data["blockers"],
        )

    def test_status_summary_includes_pending_id(self):
        LoanService.sign_customer_contract(self.customer)
        self.client.force_authenticate(user=self.staff)
        summary = self.client.get("/api/loans/status-summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertGreaterEqual(summary.data["pending_id"], 1)

    def test_unsigned_contract_still_blocks_funding_before_id_check(self):
        self.loan.status = "pending_funding"
        self.loan.approved_at = timezone.now()
        self.loan.save(update_fields=["status", "approved_at", "updated_at"])
        blockers = funding_configuration_ready(self.loan)["blockers"]
        self.assertIn("Contract must be signed before funding.", blockers)
        self.assertIn("Government ID must be uploaded before funding.", blockers)

    def test_signed_pending_id_cannot_be_expired_as_unsigned_contract(self):
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        with self.assertRaisesMessage(
            ValueError,
            "Only unsigned approved contracts can be cancelled as expired.",
        ):
            LoanService.expire_unsigned_contract(self.loan, expired_by=self.staff)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")

    def test_fund_loan_blocks_signed_approved_loan_without_id(self):
        add_government_id(self.customer)
        LoanService.sign_customer_contract(self.customer)
        self.loan.refresh_from_db()
        LoanService.approve_loan(self.loan, approved_by=self.staff)
        self.customer.documents.filter(
            document_type=CustomerDocument.TYPE_GOVERNMENT_ID
        ).delete()
        self.loan.refresh_from_db()
        with self.assertRaisesMessage(
            ValueError,
            "Government ID must be uploaded before funding.",
        ):
            LoanService.fund_loan(self.loan, method="eft", user=self.staff)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")

    def test_complete_pending_id_is_noop_until_government_id_exists(self):
        LoanService.sign_customer_contract(self.customer)
        LoanService.complete_pending_id(self.customer)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")
        add_government_id(self.customer)
        LoanService.complete_pending_id(self.customer)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending")

    def _portal_sign_payload(self, typed_name="fghmn"):
        return {
            "typed_name": typed_name,
            "accepted_terms": True,
            "accepted_credit_check": True,
            "accepted_banking_review": True,
            "accepted_electronic_signature": True,
        }

    def test_portal_sign_without_id_then_preview_and_analysis_succeed(self):
        """Client signs, reloads the signed agreement, then continue-to-dashboard."""
        self.client.force_authenticate(user=self.portal_user)
        sign = self.client.post(
            "/api/portal/me/sign-contract/",
            self._portal_sign_payload(),
            format="json",
        )
        self.assertEqual(sign.status_code, 200, sign.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")

        preview = self.client.get("/api/portal/me/contract-preview/")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["status"], "signed")
        self.assertEqual(preview.data["typed_name"], "fghmn")
        self.assertIn("APPLICATION CHANNEL:</strong> Landing", preview.data["agreement_text"])

        dashboard = self.client.get("/api/portal/me/dashboard/")
        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertEqual(dashboard.data["portal_state"], "id_required")

        analysis = self.client.post("/api/portal/me/run-analysis/", {}, format="json")
        self.assertEqual(analysis.status_code, 200, analysis.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_id")
        self.assertEqual(analysis.data["status"], "pending_id")

    def test_portal_sign_approved_with_id_then_analysis_keeps_pending_funding(self):
        add_government_id(self.customer)
        self.loan.status = "pending_funding"
        self.loan.approved_at = timezone.now()
        self.loan.save(update_fields=["status", "approved_at", "updated_at"])
        approved_at = self.loan.approved_at

        self.client.force_authenticate(user=self.portal_user)
        sign = self.client.post(
            "/api/portal/me/sign-contract/",
            self._portal_sign_payload(),
            format="json",
        )
        self.assertEqual(sign.status_code, 200, sign.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertTrue(self.loan.contract_signed)

        preview = self.client.get("/api/portal/me/contract-preview/")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["status"], "signed")

        analysis = self.client.post("/api/portal/me/run-analysis/", {}, format="json")
        self.assertEqual(analysis.status_code, 200, analysis.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending_funding")
        self.assertEqual(self.loan.approved_at, approved_at)
        self.assertEqual(analysis.data["status"], "pending_funding")
        self.assertIsNone(self.loan.ai_decision)

    def test_portal_sign_with_id_then_analysis_on_pending_review(self):
        add_government_id(self.customer)
        self.client.force_authenticate(user=self.portal_user)
        sign = self.client.post(
            "/api/portal/me/sign-contract/",
            self._portal_sign_payload(),
            format="json",
        )
        self.assertEqual(sign.status_code, 200, sign.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending")

        analysis = self.client.post("/api/portal/me/run-analysis/", {}, format="json")
        self.assertEqual(analysis.status_code, 200, analysis.data)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, "pending")
        self.assertIn(self.loan.ai_decision, ["approved", "declined", "review_required"])
