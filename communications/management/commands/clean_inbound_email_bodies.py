from django.core.management.base import BaseCommand

from communications.models import Communication
from communications.services.inbound_email import html_to_text


class Command(BaseCommand):
    help = "Convert raw HTML inbound email content into readable plain text."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        queryset = Communication.objects.filter(
            type="email",
            direction="inbound",
            content__icontains="<html",
        ).order_by("-created_at")[: options["limit"]]

        updated = 0
        for communication in queryset:
            cleaned = html_to_text(communication.content)
            if cleaned and cleaned != communication.content:
                if not communication.html_content:
                    communication.html_content = communication.content
                communication.content = cleaned
                communication.save(update_fields=["content", "html_content"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"Cleaned {updated} inbound email bodies."))
