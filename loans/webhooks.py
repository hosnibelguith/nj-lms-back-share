import json

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CollectionPayment, FundedPayment, WebhookEvent
from .zumrails import (
    ALL_FAILURE_EVENTS,
    TERMINAL_FAILURE_STATUSES,
    add_business_days,
    apply_collection_failure,
    log_activity,
    payload_hash,
    verify_zumrails_signature,
)


def _transaction_id(event_data):
    return event_data.get("Id") or event_data.get("TransactionId")


def _failure_reason(event_data, fallback="Failed"):
    return event_data.get("FailedTransactionEvent") or event_data.get("Event") or event_data.get("Status") or fallback


def _is_failed_status(status_value):
    return isinstance(status_value, str) and "Failed" in status_value


def _is_returned_or_rejected(status_value):
    return status_value in TERMINAL_FAILURE_STATUSES


class ZumRailsWebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        raw_payload = request.body
        signature = request.headers.get("zumrails-signature")

        if not verify_zumrails_signature(raw_payload, signature):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            data = json.loads(raw_payload.decode("utf-8"))
        except json.JSONDecodeError:
            return Response({"error": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)

        event_type = data.get("Type") or ""
        event_data = data.get("Data") or {}
        processor_transaction_id = _transaction_id(event_data)
        event_name = event_data.get("Event") or event_data.get("Status") or event_data.get("TransactionStatus")
        digest = payload_hash(raw_payload)

        if processor_transaction_id and event_name and WebhookEvent.objects.filter(
            processor_transaction_id=processor_transaction_id,
            webhook_type=event_type,
            event_name=event_name,
            processed_at__isnull=False,
        ).exists():
            return Response({"message": "Duplicate webhook ignored"}, status=status.HTTP_200_OK)

        webhook_event, created = WebhookEvent.objects.get_or_create(
            payload_hash=digest,
            defaults={
                "processor_transaction_id": processor_transaction_id,
                "webhook_type": event_type,
                "event_name": event_name,
                "payload": data,
            },
        )

        if not created and webhook_event.processed_at:
            return Response({"message": "Duplicate webhook ignored"}, status=status.HTTP_200_OK)

        try:
            if event_type == "TransactionEvent":
                self._process_transaction_event(processor_transaction_id, event_name)
            elif event_type == "Transaction":
                status_value = event_data.get("Status") or event_data.get("TransactionStatus")
                self._process_transaction(processor_transaction_id, status_value, event_data)

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at"])
        except Exception:
            webhook_event.save(update_fields=["payload"])
            raise

        return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)

    def _process_transaction_event(self, processor_transaction_id, event_name):
        if not processor_transaction_id:
            return

        collection = CollectionPayment.objects.select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if collection:
            history = collection.event_history if isinstance(collection.event_history, list) else []
            history.append({"event": event_name, "timestamp": timezone.now().isoformat()})
            collection.event_history = history

            if event_name in ALL_FAILURE_EVENTS:
                apply_collection_failure(collection, reason=event_name, status="failed")
                collection.event_history = history
                collection.save(update_fields=["event_history", "updated_at"])
                log_activity(
                    collection.loan,
                    "payment_failed",
                    "Collection Failed",
                    event_name,
                    metadata={"collection_payment_id": str(collection.id)},
                )
            else:
                collection.save(update_fields=["event_history", "updated_at"])
            return

        funding = FundedPayment.objects.select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if funding and event_name in ALL_FAILURE_EVENTS:
            funding.zum_status = event_name
            funding.mark_failed(event_name)
            log_activity(
                funding.loan,
                "system",
                "Funding Failed",
                event_name,
                metadata={"funded_payment_id": str(funding.id)},
            )

    def _process_transaction(self, processor_transaction_id, status_value, event_data):
        if not processor_transaction_id:
            return

        collection = CollectionPayment.objects.select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if collection:
            if status_value == "Completed":
                collection.zum_status = status_value
                if not collection.settlement_due_at:
                    collection.settlement_due_at = add_business_days(timezone.now(), 4)
                collection.save(update_fields=["zum_status", "settlement_due_at", "updated_at"])
                return

            if _is_failed_status(status_value) or _is_returned_or_rejected(status_value):
                reason = _failure_reason(event_data, status_value)
                failure_status = "returned" if status_value == "Returned" else "failed"
                if status_value == "Rejected":
                    failure_status = "rejected"
                apply_collection_failure(collection, reason=reason, status=failure_status)
                collection.zum_status = status_value
                collection.save(update_fields=["zum_status", "updated_at"])
                log_activity(
                    collection.loan,
                    "payment_failed",
                    "Collection Failed",
                    reason,
                    metadata={"collection_payment_id": str(collection.id)},
                )
                return

            collection.zum_status = status_value
            collection.save(update_fields=["zum_status", "updated_at"])
            return

        funding = FundedPayment.objects.select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if funding:
            funding.zum_status = status_value
            if status_value == "Completed":
                funding.mark_completed()
                if funding.loan.status != "active":
                    funding.loan.mark_funding_completed(
                        method=funding.method,
                        reference=funding.processor_transaction_id or "",
                    )
                log_activity(
                    funding.loan,
                    "loan_funded",
                    "Funding Completed",
                    "ZūmRails confirmed funding completion.",
                    metadata={"funded_payment_id": str(funding.id)},
                )
                return

            if status_value == "Returned":
                reason = _failure_reason(event_data, status_value)
                funding.zum_status = status_value
                funding.mark_returned(reason)
                log_activity(
                    funding.loan,
                    "system",
                    "Funding Returned",
                    reason,
                    metadata={"funded_payment_id": str(funding.id)},
                )
                return

            if _is_failed_status(status_value):
                reason = _failure_reason(event_data, status_value)
                funding.zum_status = status_value
                funding.mark_failed(reason)
                log_activity(
                    funding.loan,
                    "system",
                    "Funding Failed",
                    reason,
                    metadata={"funded_payment_id": str(funding.id)},
                )
                return

            funding.save(update_fields=["zum_status", "updated_at"])
