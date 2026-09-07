from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import Customer, CustomerDocument, User


@override_settings(MEDIA_ROOT="/tmp/nj-lms-test-media")
class CustomerDocumentTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="docs-staff@example.com",
            password="password123",
            full_name="Docs Staff",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.portal_user = User.objects.create_user(
            email="docs-customer@example.com",
            password="password123",
            full_name="Docs Customer",
            user_type="customer",
        )
        self.customer = Customer.objects.create(
            portal_user=self.portal_user,
            first_name="Docs",
            last_name="Customer",
            email="docs-customer@example.com",
            phone="4165550303",
            province="ON",
            status="active",
        )

    def _pdf(self, name="cheque.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 test", content_type="application/pdf")

    def test_portal_can_upload_supporting_docs_but_not_government_id(self):
        self.client.force_authenticate(user=self.portal_user)
        created = self.client.post(
            "/api/portal/me/documents/",
            {"document_type": "void_cheque", "file": self._pdf()},
            format="multipart",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["document_type"], "void_cheque")
        self.assertNotIn("government_id", created.data["document_type"])

        blocked = self.client.post(
            "/api/portal/me/documents/",
            {"document_type": "government_id", "file": self._pdf("id.pdf")},
            format="multipart",
        )
        self.assertEqual(blocked.status_code, 400)

        listed = self.client.get("/api/portal/me/documents/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data), 1)

        download = self.client.get(f"/api/portal/me/documents/{created.data['id']}/file/")
        self.assertEqual(download.status_code, 200)

    def test_staff_can_upload_government_id(self):
        self.client.force_authenticate(user=self.staff)
        created = self.client.post(
            f"/api/customers/{self.customer.id}/documents/",
            {"document_type": "government_id", "file": self._pdf("id.pdf")},
            format="multipart",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["document_type"], "government_id")
        self.assertTrue(
            CustomerDocument.objects.filter(
                customer=self.customer,
                document_type=CustomerDocument.TYPE_GOVERNMENT_ID,
            ).exists()
        )
        listed = self.client.get(f"/api/customers/{self.customer.id}/documents/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data), 1)
        download = self.client.get(
            f"/api/customers/{self.customer.id}/documents/{created.data['id']}/file/"
        )
        self.assertEqual(download.status_code, 200)
