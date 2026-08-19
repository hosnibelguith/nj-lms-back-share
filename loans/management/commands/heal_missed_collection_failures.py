"""Apply missed Zūm AR collection failures for specific processor transaction ids."""

from django.core.management.base import BaseCommand

from loans.models import CollectionPayment
from loans.zumrails import SettlementService


class Command(BaseCommand):
    help = (
        "Look up Zūm for the given AR collection transaction ids and apply "
        "Failed/Returned/Rejected locally. Does not auto-complete collections."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "transaction_ids",
            nargs="+",
            help="Zūm processor transaction UUID(s), full or unique prefix.",
        )

    def handle(self, *args, **options):
        healed = 0
        for tx_id in options["transaction_ids"]:
            tx_id = (tx_id or "").strip()
            if not tx_id:
                continue
            collection = CollectionPayment.objects.filter(
                processor_transaction_id=tx_id,
            ).first()
            if collection is None and len(tx_id) >= 12:
                matches = list(
                    CollectionPayment.objects.filter(
                        processor_transaction_id__startswith=tx_id,
                    )[:2]
                )
                if len(matches) == 1:
                    collection = matches[0]
            if collection is None:
                self.stderr.write(f"No collection payment for {tx_id}")
                continue
            previous = collection.status
            synced = SettlementService.sync_from_zum(collection)
            if (
                synced.status in ("failed", "returned", "rejected")
                and previous != synced.status
            ):
                healed += 1
                self.stdout.write(
                    f"Healed {synced.processor_transaction_id} -> {synced.status}"
                )
            else:
                self.stdout.write(
                    f"Unchanged {synced.processor_transaction_id} "
                    f"status={synced.status} zum={synced.zum_status}"
                )
        self.stdout.write(f"Healed {healed} collection(s).")
