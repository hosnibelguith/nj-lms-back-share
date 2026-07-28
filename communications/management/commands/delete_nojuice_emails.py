from django.core.management.base import BaseCommand
from django.db.models import Q

from communications.models import Communication


class Command(BaseCommand):
    help = "Delete old NoJuice email communication records from the staff dashboard."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually delete matching records. Without this flag the command is a dry run.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Number of matching records to print as a sample.",
        )

    def handle(self, *args, **options):
        queryset = Communication.objects.filter(type="email").filter(
            Q(from_address__icontains="nojuice")
            | Q(to_address__icontains="nojuice")
            | Q(subject__icontains="nojuice")
            | Q(content__icontains="nojuice")
            | Q(html_content__icontains="nojuice")
        )

        count = queryset.count()
        self.stdout.write(f"Matched {count} NoJuice email communication records.")

        for communication in queryset.order_by("-created_at")[: options["limit"]]:
            self.stdout.write(
                f"- {communication.id} | {communication.direction} | "
                f"from={communication.from_address or '-'} | "
                f"to={communication.to_address or '-'} | "
                f"subject={communication.subject or '-'}"
            )

        if not options["execute"]:
            self.stdout.write(self.style.WARNING("Dry run only. Re-run with --execute to delete."))
            return

        deleted_count, deleted_by_model = queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} objects: {deleted_by_model}"
            )
        )
