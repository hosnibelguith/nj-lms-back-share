from django.core.management.base import BaseCommand

from accounts.models import Customer
from activity.models import ActivityHistory
from banking.models import BankAccount, BankConnection
from banking.tasks import (
    UNSUPPORTED_IBV_INSTITUTIONS,
    UNSUPPORTED_IBV_REASON_CODE,
    UNSUPPORTED_INSTITUTION_MESSAGE,
    _normalize_institution_number,
    send_banking_retry_email,
)


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
        connection_institutions = {}

        for account in BankAccount.objects.select_related("connection"):
            institution = _normalize_institution_number(account.institution_number)
            if institution in UNSUPPORTED_IBV_INSTITUTIONS:
                connection_institutions.setdefault(account.connection_id, set()).add(institution)

        connections = list(
            BankConnection.objects.select_related("customer").filter(
                id__in=connection_institutions.keys()
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
        customer_institutions = {
            customer_id: sorted({
                institution
                for connection in connections
                if connection.customer_id == customer_id
                for institution in connection_institutions.get(connection.id, set())
            })
            for customer_id in affected_customer_ids
        }
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
            institutions = customer_institutions.get(customer_id, [])
            reason = (
                f"{UNSUPPORTED_INSTITUTION_MESSAGE} "
                f"Unsupported institution number(s): {', '.join(institutions)}."
            )
            customer.banking_verified = False
            if customer.onboarding_stage != "banking_verification":
                customer.onboarding_stage = "banking_verification"
            customer.save(update_fields=["banking_verified", "onboarding_stage", "updated_at"])
            ActivityHistory.objects.create(
                customer=customer,
                type="system",
                title="Banking Verification Reset",
                description=reason,
                created_by="system",
                metadata={
                    "source": "unsupported_ibv_cleanup",
                    "reason_code": UNSUPPORTED_IBV_REASON_CODE,
                    "unsupported_institutions": institutions,
                },
            )
            send_banking_retry_email.delay(str(customer.id), reason)
            reset_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {len(connections)} unsupported IBV connection(s); reset {reset_count} customer(s)."
            )
        )
