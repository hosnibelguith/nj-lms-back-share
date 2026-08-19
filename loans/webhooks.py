import json
import logging
import re
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
    apply_funded_payment_zum_status,
    collection_failure_status_from_zum,
    extract_zum_transaction_fields,
    is_terminal_zum_collection_failure,
    is_zum_failed_status,
    log_activity,
    normalize_zum_status,
    payload_hash,
    reason_implies_interac_security_failure,
    release_funding_locks,
    verify_zumrails_signature,
)

logger = logging.getLogger(__name__)


def _transaction_id(event_data):
    fields = extract_zum_transaction_fields(event_data)
    if fields.get("transaction_id"):
        return fields["transaction_id"]
    transaction_data = event_data.get("Transaction")
    if not isinstance(transaction_data, dict):
        transaction_data = {}
    return (
        event_data.get("Id")
        or event_data.get("TransactionId")
        or transaction_data.get("Id")
    )


def _failure_reason(event_data, fallback="Failed"):
    fields = extract_zum_transaction_fields(event_data)
    return fields.get("reason") or fields.get("status") or fallback


def _is_failure_event(event_name):
    if not isinstance(event_name, str) or not event_name.strip():
        return False
    compact = re.sub(r"[\s_\-]+", "", event_name)
    return (
        event_name in ALL_FAILURE_EVENTS
        or event_name.startswith(("EftFailed", "InteracFailed"))
        or compact.startswith(("EftFailed", "InteracFailed"))
        or reason_implies_interac_security_failure(event_name)
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


def _payload_is_collection_failure(event_name, event_data, status_value=None):
    fields = extract_zum_transaction_fields(event_data)
    resolved_status = status_value or fields.get("status")
    reason = fields.get("reason") or event_name
    return _is_failure_event(event_name) or is_terminal_zum_collection_failure(
        resolved_status,
        reason,
    )


def _find_collection(processor_transaction_id, event_data):
    qs = CollectionPayment.objects.select_for_update().select_related("loan")
    collection = None
    if processor_transaction_id:
        collection = qs.filter(processor_transaction_id=processor_transaction_id).first()
    if collection is None:
        client_transaction_id = _client_transaction_uuid(event_data)
        if client_transaction_id:
            collection = qs.filter(pk=client_transaction_id).first()
            if (
                collection
                and processor_transaction_id
                and not collection.processor_transaction_id
            ):
                collection.processor_transaction_id = processor_transaction_id
                collection.save(update_fields=["processor_transaction_id", "updated_at"])
    return collection


def _unmatched_failure_should_reprocess(event_name, event_data, processor_transaction_id):
    """True when a duplicate failure webhook still has a local collection to heal."""
    if not _payload_is_collection_failure(event_name, event_data):
        return False
    collection = None
    if processor_transaction_id:
        collection = CollectionPayment.objects.filter(
            processor_transaction_id=processor_transaction_id
        ).first()
    if collection is None:
        client_transaction_id = _client_transaction_uuid(event_data)
        if client_transaction_id:
            collection = CollectionPayment.objects.filter(pk=client_transaction_id).first()
    if collection is None:
        return False
    return collection.status not in ("failed", "returned", "rejected", "cancelled")


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
        fields = extract_zum_transaction_fields(event_data)
        transaction_data = event_data.get("Transaction")
        client_transaction_id = event_data.get("ClientTransactionId")
        if not client_transaction_id and isinstance(transaction_data, dict):
            client_transaction_id = transaction_data.get("ClientTransactionId")

        logger.info(
            "ZumRails webhook received type=%s event=%s status=%s tx=%s client_tx=%s",
            event_type,
            event_name,
            fields.get("status") or event_data.get("Status") or event_data.get("TransactionStatus"),
            processor_transaction_id,
            client_transaction_id,
        )

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
            if not _unmatched_failure_should_reprocess(
                event_name,
                event_data,
                processor_transaction_id,
            ):
                return Response({"message": "Duplicate webhook ignored"}, status=status.HTTP_200_OK)

        try:
            if event_type == "TransactionEvent":
                self._process_transaction_event(
                    processor_transaction_id,
                    event_name,
                    event_data,
                )
            elif event_type == "Transaction" or (
                not event_type
                and (fields.get("status") or event_data.get("TransactionStatus") or event_data.get("Status"))
            ):
                status_value = (
                    fields.get("status")
                    or event_data.get("TransactionStatus")
                    or event_data.get("Status")
                )
                self._process_transaction(processor_transaction_id, status_value, event_data)
            elif _is_failure_event(event_name):
                self._process_transaction_event(
                    processor_transaction_id,
                    event_name,
                    event_data,
                )

            webhook_event.processed_at = timezone.now()
            webhook_event.save(update_fields=["processed_at"])
        except Exception:
            webhook_event.save(update_fields=["payload"])
            raise

        return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)

    def _process_transaction_event(self, processor_transaction_id, event_name, event_data):
        if not processor_transaction_id and not _client_transaction_uuid(event_data):
            return

        event_timestamp = (
            event_data.get("CreatedAt")
            or event_data.get("Timestamp")
            or timezone.now().isoformat()
        )
        history_item = {"event": event_name, "timestamp": event_timestamp}

        collection = _find_collection(processor_transaction_id, event_data)
        if collection:
            logger.info(
                "ZumRails webhook matched collection id=%s status=%s zum_status=%s event=%s tx=%s",
                collection.id,
                collection.status,
                collection.zum_status,
                event_name,
                processor_transaction_id,
            )
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

        funding = None
        if processor_transaction_id:
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
            logger.info(
                "ZumRails webhook matched funding id=%s status=%s zum_status=%s event=%s tx=%s",
                funding.id,
                funding.status,
                funding.zum_status,
                event_name,
                processor_transaction_id,
            )
            history = funding.event_history if isinstance(funding.event_history, list) else []
            history.append(history_item)
            funding.event_history = history
            funding.save(update_fields=["event_history", "updated_at"])
            fields = extract_zum_transaction_fields(event_data)
            reason = _failure_reason(event_data, event_name or "Failed")
            if _is_failure_event(event_name) or reason_implies_interac_security_failure(reason):
                apply_funded_payment_zum_status(
                    funding,
                    status_value="Failed",
                    reason=reason,
                )
                return
            # Zum sometimes reports completion only as a TransactionEvent.
            event_status = normalize_zum_status(
                fields.get("status") or event_name
            )
            if event_status == "Completed" or (
                isinstance(event_name, str) and event_name.strip().lower() == "completed"
            ):
                apply_funded_payment_zum_status(
                    funding,
                    status_value="Completed",
                    reason=None,
                )

    def _process_transaction(self, processor_transaction_id, status_value, event_data):
        if not processor_transaction_id and not _client_transaction_uuid(event_data):
            return

        collection = _find_collection(processor_transaction_id, event_data)
        if collection:
            logger.info(
                "ZumRails transaction matched collection id=%s status=%s zum_status=%s incoming_status=%s tx=%s",
                collection.id,
                collection.status,
                collection.zum_status,
                status_value,
                processor_transaction_id,
            )
            fields = extract_zum_transaction_fields(event_data)
            resolved_status = status_value or fields.get("status")
            reason = fields.get("reason") or resolved_status
            if is_terminal_zum_collection_failure(resolved_status, reason):
                failure_status = collection_failure_status_from_zum(resolved_status)
                was_terminal = collection.status in ("failed", "returned", "rejected")
                if not was_terminal:
                    apply_collection_failure(
                        collection,
                        reason=reason or resolved_status or "Failed",
                        status=failure_status,
                    )
                if is_zum_failed_status(resolved_status) or normalize_zum_status(resolved_status) in TERMINAL_FAILURE_STATUSES:
                    collection.zum_status = resolved_status
                else:
                    collection.zum_status = "Failed"
                collection.save(update_fields=["zum_status", "updated_at"])
                if not was_terminal:
                    log_activity(
                        collection.loan,
                        "payment_failed",
                        "Collection Failed",
                        reason or resolved_status,
                        metadata={"collection_payment_id": str(collection.id)},
                    )
                return

            if resolved_status == "Completed":
                collection.zum_status = resolved_status
                if not collection.settlement_due_at:
                    collection.settlement_due_at = add_business_days(collection.initiated_at, 4)
                collection.save(update_fields=["zum_status", "settlement_due_at", "updated_at"])
                return

            collection.zum_status = resolved_status
            collection.save(update_fields=["zum_status", "updated_at"])
            return

        funding = None
        if processor_transaction_id:
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
            logger.info(
                "ZumRails transaction matched funding id=%s status=%s zum_status=%s incoming_status=%s tx=%s",
                funding.id,
                funding.status,
                funding.zum_status,
                status_value,
                processor_transaction_id,
            )
            fields = extract_zum_transaction_fields(event_data)
            resolved_status = status_value or fields.get("status")
            reason = _failure_reason(event_data, resolved_status or "Failed")
            apply_funded_payment_zum_status(
                funding,
                status_value=resolved_status,
                reason=reason if resolved_status != "Completed" else None,
            )
