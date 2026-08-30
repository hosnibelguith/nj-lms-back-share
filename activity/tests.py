from rest_framework.test import APITestCase

from accounts.models import Customer, User
from activity.models import ActivityHistory, Comment


class CommentTimelineTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="notes-agent@example.com",
            password="password123",
            full_name="Notes Agent",
            user_type="staff",
            is_staff=True,
            permission_level=4,
        )
        self.customer = Customer.objects.create(
            first_name="Simon",
            last_name="Duku",
            email="simon.duku.notes@example.com",
            phone="4165550191",
            province="ON",
            status="active",
        )
        self.client.force_authenticate(self.staff)

    def test_new_note_is_written_to_activity_history_and_timeline(self):
        response = self.client.post(
            "/api/comments/",
            {
                "customer": str(self.customer.id),
                "content": "Called customer about Interac",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Comment.objects.filter(customer=self.customer).count(), 1)
        row = ActivityHistory.objects.get(customer=self.customer, type="comment")
        self.assertEqual(row.description, "Called customer about Interac")
        self.assertEqual(row.created_by, str(self.staff.id))

        timeline = self.client.get(
            f"/api/activities/timeline/?customer_id={self.customer.id}"
        )
        self.assertEqual(timeline.status_code, 200, timeline.data)
        notes = [item for item in timeline.data if item["type"] == "comment"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["description"], "Called customer about Interac")
        self.assertEqual(notes[0]["created_by_name"], "Notes Agent")

    def test_pinning_a_note_does_not_duplicate_history(self):
        comment = Comment.objects.create(
            customer=self.customer,
            content="First note",
            created_by=self.staff,
        )
        self.assertEqual(
            ActivityHistory.objects.filter(customer=self.customer, type="comment").count(),
            1,
        )
        comment.is_pinned = True
        comment.save()
        self.assertEqual(
            ActivityHistory.objects.filter(customer=self.customer, type="comment").count(),
            1,
        )
