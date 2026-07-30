from django.core.management.base import BaseCommand

from accounts.models import Customer
from banking.models import BankAccount, BankConnection
from banking.tasks import UNSUPPORTED_IBV_INSTITUTIONS, _normalize_institution_number


class Command(BaseCommand):
    help = "Delete IBV connections containing unsupported institution numbers 621 or 623."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report affected connections without deleting them.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        unsupported_connection_ids = set()

        for account in BankAccount.objects.select_related("connection"):
            institution = _normalize_institution_number(account.institution_number)
            if institution in UNSUPPORTED_IBV_INSTITUTIONS:
                unsupported_connection_ids.add(account.connection_id)

        connections = list(
            BankConnection.objects.select_related("customer").filter(
                id__in=unsupported_connection_ids
            )
        )

        if dry_run:
            self.stdout.write(
                f"Would delete {len(connections)} unsupported IBV connection(s)."
            )
            for connection in connections:
                self.stdout.write(
                    f"  customer={connection.customer.email} connection={connection.id}"
                )
            return

        affected_customer_ids = {connection.customer_id for connection in connections}
        for connection in connections:
            self.stdout.write(
                "Deleting unsupported IBV connection "
                f"customer={connection.customer.email} connection={connection.id}"
            )
            connection.delete()

        reset_count = 0
        for customer_id in affected_customer_ids:
            valid_connection_exists = BankConnection.objects.filter(
                customer_id=customer_id,
                is_active=True,
                sync_status="synced",
                accounts__isnull=False,
            ).distinct().exists()

            if valid_connection_exists:
                continue

            customer = Customer.objects.get(id=customer_id)
            customer.banking_verified = False
            if customer.onboarding_stage != "banking_verification":
                customer.onboarding_stage = "banking_verification"
            customer.save(update_fields=["banking_verified", "onboarding_stage", "updated_at"])
            reset_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {len(connections)} unsupported IBV connection(s); reset {reset_count} customer(s)."
            )
        )
