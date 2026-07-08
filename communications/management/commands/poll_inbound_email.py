from django.core.management.base import BaseCommand, CommandError

from communications.services.inbound_email import poll_configured_inbound_emails


class Command(BaseCommand):
    help = "Poll configured inbound email provider and create inbound email communications."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--mailbox", default=None)
        parser.add_argument("--provider", choices=["graph", "imap"], default=None)
        parser.add_argument(
            "--no-mark-seen",
            action="store_true",
            help="Do not mark imported matching messages as seen.",
        )

    def handle(self, *args, **options):
        try:
            result = poll_configured_inbound_emails(
                provider=options["provider"],
                limit=options["limit"],
                mailbox=options["mailbox"],
                mark_seen=not options["no_mark_seen"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Inbound email poll complete: {result.as_dict()}"))
