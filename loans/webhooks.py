import json
import uuid

from django.db import transaction
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
    release_funding_locks,
    verify_zumrails_signature,
)


def _transaction_id(event_data):
    transaction_data = event_data.get("Transaction")
    if not isinstance(transaction_data, dict):
        transaction_data = {}
    return (
        event_data.get("Id")
        or event_data.get("TransactionId")
        or transaction_data.get("Id")
    )


def _failure_reason(event_data, fallback="Failed"):
    return event_data.get("FailedTransactionEvent") or event_data.get("Event") or event_data.get("Status") or fallback


def _is_failed_status(status_value):
    return isinstance(status_value, str) and "Failed" in status_value


def _is_returned_or_rejected(status_value):
    return status_value in TERMINAL_FAILURE_STATUSES


def _is_failure_event(event_name):
    return (
        event_name in ALL_FAILURE_EVENTS
        or (
            isinstance(event_name, str)
            and event_name.startswith(("EftFailed", "InteracFailed"))
        )
    )


def _client_transaction_uuid(event_data):
    value = event_data.get("ClientTransactionId")
    if not value:
        transaction_data = event_data.get("Transaction")
        if isinstance(transaction_data, dict):
            value = transaction_data.get("ClientTransactionId")
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _reopen_loan_after_funding_failure(funding):
    loan = funding.loan
    if loan.status == "active":
        loan.status = "pending_funding"
        loan.is_active = False
        loan.funded_at = None
        loan.save(update_fields=[
            "status",
            "is_active",
            "funded_at",
            "updated_at",
        ])
    release_funding_locks(loan)


class ZumRailsWebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
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
        nested_data = data.get("Data")
        event_data = nested_data if isinstance(nested_data, dict) else data
        processor_transaction_id = _transaction_id(event_data)
        event_name = event_data.get("Event") or event_data.get("Status") or event_data.get("TransactionStatus")
        digest = payload_hash(raw_payload)

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
                self._process_transaction_event(
                    processor_transaction_id,
                    event_name,
                    event_data,
                )
            elif event_type == "Transaction":
                status_value = event_data.get("Status") or event_data.get("TransactionStatus")
                self._process_transaction(processor_transaction_id, status_value, event_data)

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at"])
        except Exception:
            webhook_event.save(update_fields=["payload"])
            raise

        return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)

    def _process_transaction_event(self, processor_transaction_id, event_name, event_data):
        if not processor_transaction_id:
            return

        event_timestamp = (
            event_data.get("CreatedAt")
            or event_data.get("Timestamp")
            or timezone.now().isoformat()
        )
        history_item = {"event": event_name, "timestamp": event_timestamp}

        collection = CollectionPayment.objects.select_for_update().select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if not collection:
            client_transaction_id = _client_transaction_uuid(event_data)
            if client_transaction_id:
                collection = CollectionPayment.objects.select_for_update().select_related(
                    "loan"
                ).filter(pk=client_transaction_id).first()
                if collection and not collection.processor_transaction_id:
                    collection.processor_transaction_id = processor_transaction_id
                    collection.save(update_fields=["processor_transaction_id", "updated_at"])
        if collection:
            history = collection.event_history if isinstance(collection.event_history, list) else []
            history.append(history_item)
            collection.event_history = history

            if _is_failure_event(event_name):
                was_terminal = collection.status in ("failed", "returned", "rejected")
                if not was_terminal:
                    apply_collection_failure(collection, reason=event_name, status="failed")
                collection.event_history = history
                collection.save(update_fields=["event_history", "updated_at"])
                if not was_terminal:
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

        funding = FundedPayment.objects.select_for_update().select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if not funding:
            client_transaction_id = _client_transaction_uuid(event_data)
            if client_transaction_id:
                funding = FundedPayment.objects.select_for_update().select_related(
                    "loan"
                ).filter(pk=client_transaction_id).first()
                if funding and not funding.processor_transaction_id:
                    funding.processor_transaction_id = processor_transaction_id
                    funding.reference = processor_transaction_id
                    funding.save(update_fields=[
                        "processor_transaction_id",
                        "reference",
                        "updated_at",
                    ])
        if funding:
            history = funding.event_history if isinstance(funding.event_history, list) else []
            history.append(history_item)
            funding.event_history = history
            if _is_failure_event(event_name):
                was_terminal = funding.status in ("failed", "returned")
                funding.zum_status = event_name
                if not was_terminal:
                    funding.mark_failed(event_name)
                    _reopen_loan_after_funding_failure(funding)
                funding.event_history = history
                funding.save(update_fields=["event_history", "updated_at"])
                if not was_terminal:
                    log_activity(
                        funding.loan,
                        "system",
                        "Funding Failed",
                        event_name,
                        metadata={"funded_payment_id": str(funding.id)},
                    )
            else:
                funding.save(update_fields=["event_history", "updated_at"])

    def _process_transaction(self, processor_transaction_id, status_value, event_data):
        if not processor_transaction_id:
            return

        collection = CollectionPayment.objects.select_for_update().select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if not collection:
            client_transaction_id = _client_transaction_uuid(event_data)
            if client_transaction_id:
                collection = CollectionPayment.objects.select_for_update().select_related(
                    "loan"
                ).filter(pk=client_transaction_id).first()
                if collection and not collection.processor_transaction_id:
                    collection.processor_transaction_id = processor_transaction_id
                    collection.save(update_fields=["processor_transaction_id", "updated_at"])
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
                was_terminal = collection.status in ("failed", "returned", "rejected")
                if not was_terminal:
                    apply_collection_failure(collection, reason=reason, status=failure_status)
                collection.zum_status = status_value
                collection.save(update_fields=["zum_status", "updated_at"])
                if not was_terminal:
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

        funding = FundedPayment.objects.select_for_update().select_related("loan").filter(
            processor_transaction_id=processor_transaction_id
        ).first()
        if not funding:
            client_transaction_id = _client_transaction_uuid(event_data)
            if client_transaction_id:
                funding = FundedPayment.objects.select_for_update().select_related(
                    "loan"
                ).filter(pk=client_transaction_id).first()
                if funding and not funding.processor_transaction_id:
                    funding.processor_transaction_id = processor_transaction_id
                    funding.reference = processor_transaction_id
                    funding.save(update_fields=[
                        "processor_transaction_id",
                        "reference",
                        "updated_at",
                    ])
        if funding:
            funding.zum_status = status_value
            if status_value == "Completed":
                was_completed = funding.status == "completed"
                if funding.status == "processing":
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
                was_returned = funding.status == "returned"
                funding.zum_status = status_value
                if not was_returned:
                    funding.mark_returned(reason)
                    _reopen_loan_after_funding_failure(funding)
                    log_activity(
                        funding.loan,
                        "system",
                        "Funding Returned",
                        reason,
                        metadata={"funded_payment_id": str(funding.id)},
                    )
                return

            if _is_failed_status(status_value) or status_value == "Rejected":
                reason = _failure_reason(event_data, status_value)
                was_failed = funding.status == "failed"
                funding.zum_status = status_value
                if not was_failed:
                    funding.mark_failed(reason)
                    _reopen_loan_after_funding_failure(funding)
                    log_activity(
                        funding.loan,
                        "system",
                        "Funding Failed",
                        reason,
                        metadata={"funded_payment_id": str(funding.id)},
                    )
                return

            funding.save(update_fields=["zum_status", "updated_at"])
