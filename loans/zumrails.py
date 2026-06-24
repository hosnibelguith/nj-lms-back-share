import base64
import hashlib
import hmac
import logging
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from banking.models import BankAccount

from .models import (
    CollectionPayment,
    CollectionsAccountChangeAudit,
    FundedPayment,
    FundingMethodRecommendation,
    Loan,
    Payment,
)

logger = logging.getLogger(__name__)

EFT_FAILURE_EVENTS = {
    "EftFailedInsufficientFunds",
    "EftFailedAccountClosed",
    "EftFailedCannotLocateAccount",
    "EftFailedStopPayment",
    "EftFailedNoDebitAllowed",
    "EftFailedFrozenAccount",
    "EftFailedInvalidErrorAccountNumber",
    "EftFailedRefusedNoAgreement",
    "EftFailedAgreementRevoked",
    "EftFailedPayorPayeeDeceased",
    "EftFailedNotInAccountAgreementP",
    "EftFailedNotInAccountAgreementE",
    "EftFailedNoPrenotificationP1",
    "EftFailedNoPrenotificationP2",
    "EftFailedDefaultByAFinancialInstitution",
    "EftFailedTransactionLimitExceeded",
    "EftFailedValidationRejection",
    "EftFailedTransactionNotAllowed",
}

INTERAC_FAILURE_EVENTS = {
    "InteracFailedRecipientRejected",
    "InteracFailedAuthentication",
    "InteracFailedReachedCancellationCutOff",
    "InteracFailedDebtorRejected",
    "InteracFailedFundsDepositFailed",
    "InteracFailedNameMismatch",
    "InteracFailedInvalidAccountNumber",
    "InteracFailedRequestBlockedByUser",
    "InteracFailedBulkCancellationRequest",
}

ALL_FAILURE_EVENTS = EFT_FAILURE_EVENTS | INTERAC_FAILURE_EVENTS
TERMINAL_FAILURE_STATUSES = {"Returned", "Rejected"}
COLLECTION_SETTLEMENT_BUSINESS_DAYS = 4


def verify_zumrails_signature(payload: bytes, signature: str | None) -> bool:
    secret = getattr(settings, "ZUMRAILS_WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False

    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature, expected)


def payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def add_business_days(start_dt, days: int):
    current = timezone.localtime(start_dt)
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def account_snapshot(account: BankAccount | None) -> dict:
    if not account:
        return {}
    return {
        "id": str(account.id),
        "name": account.name,
        "type": account.type,
        "currency": account.currency,
        "transit_number": account.transit_number,
        "institution_number": account.institution_number,
        "account_number": account.account_number,
        "external_id": account.external_id,
    }


def public_account_snapshot(account: BankAccount | None) -> dict:
    snapshot = account_snapshot(account)
    account_number = snapshot.get("account_number") or ""
    snapshot["account_last4"] = account_number[-4:] if account_number else ""
    snapshot.pop("account_number", None)
    return snapshot


def funding_destination_snapshot(loan: Loan, method: str, destination: dict | None = None) -> dict:
    destination = destination or {}
    configured = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}

    if method == "etransfer":
        emt = configured.get("emt") if isinstance(configured.get("emt"), dict) else {}
        email = (
            destination.get("email")
            or emt.get("email")
            or loan.funding_reference
            or getattr(loan.customer.portal_user, "flinks_email", None)
            or loan.customer.email
        )
        snapshot = {"method": "emt", "email": email}
        if emt.get("source"):
            snapshot["source"] = emt["source"]
        return snapshot

    eft = configured.get("eft") if isinstance(configured.get("eft"), dict) else {}
    account_dict = eft.get("account") if isinstance(eft.get("account"), dict) else None
    bank_account_id = destination.get("bank_account_id") or eft.get("bank_account_id")
    if bank_account_id:
        account = BankAccount.objects.filter(id=bank_account_id, customer=loan.customer).first()
        if account:
            account_dict = account_snapshot(account)
    elif not account_dict and loan.bank_account:
        account_dict = account_snapshot(loan.bank_account)

    return {"method": "eft", "account": account_dict or {}}


def funding_configuration_ready(loan: Loan) -> dict:
    configured = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}
    emt = configured.get("emt") if isinstance(configured.get("emt"), dict) else {}
    eft = configured.get("eft") if isinstance(configured.get("eft"), dict) else {}
    collections_account = loan.collections_account or loan.bank_account
    emt_configured = bool(emt.get("email"))
    eft_configured = bool(eft.get("account") or loan.bank_account)
    collections_configured = bool(collections_account)
    blockers = []
    if loan.status != "pending_funding":
        blockers.append("Loan is not pending funding.")
    if loan.funded_payments.filter(status__in=["processing", "completed"]).exists():
        blockers.append("Funding already exists for this loan.")
    if not emt_configured or not eft_configured:
        blockers.append("Funding destination required.")
    if not collections_configured:
        blockers.append("Collections account required.")
    return {
        "emt_configured": emt_configured,
        "eft_configured": eft_configured,
        "collections_account_configured": collections_configured,
        "has_active_funding": loan.funded_payments.filter(status__in=["processing", "completed"]).exists(),
        "blockers": blockers,
    }


def apply_collection_failure(collection: CollectionPayment, *, reason: str, status: str = "failed"):
    collection.status = status
    collection.failure_reason = reason
    collection.save(update_fields=["status", "failure_reason", "updated_at"])

    payment = collection.payment
    if payment and payment.status not in ("completed", "failed", "nsf", "cancelled"):
        if "InsufficientFunds" in reason:
            payment.mark_nsf()
        else:
            payment.fail(reason)


def log_activity(loan: Loan, type_value: str, title: str, description: str, created_by="system", metadata=None):
    try:
        from activity.models import ActivityHistory

        ActivityHistory.objects.create(
            customer=loan.customer,
            loan=loan,
            type=type_value,
            title=title,
            description=description,
            created_by=str(created_by) if created_by else "system",
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("Failed to log activity for loan=%s", loan.id)


class ZumRailsService:
    @staticmethod
    def _method(method: str) -> str:
        return "Interac" if method == "etransfer" else "Eft"

    @staticmethod
    def initiate_transaction(*, amount, transaction_type: str, method: str, memo: str, extra=None) -> str:
        if getattr(settings, "ZUMRAILS_DRY_RUN", False):
            return f"dryrun-{transaction_type.lower()}-{uuid.uuid4().hex}"

        api_url = getattr(settings, "ZUMRAILS_API_BASE_URL", "").rstrip("/")
        api_key = getattr(settings, "ZUMRAILS_API_KEY", "")
        if not api_url or not api_key:
            raise ValueError("ZūmRails API settings are not configured.")

        payload = {
            "Amount": str(amount),
            "Type": transaction_type,
            "Method": method,
            "Memo": memo[:100],
        }
        if extra:
            payload.update(extra)

        response = requests.post(
            f"{api_url}/transactions",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        transaction_id = data.get("Id") or data.get("TransactionId") or data.get("id")
        if not transaction_id:
            raise ValueError("ZūmRails did not return a transaction ID.")
        return transaction_id


class FundingService:
    @staticmethod
    @transaction.atomic
    def initiate(loan: Loan, *, method: str, schedule_confirmed: bool, user, destination=None, collections_account=None):
        loan = Loan.objects.select_for_update().select_related(
            "customer",
            "customer__portal_user",
            "bank_account",
            "collections_account",
        ).get(pk=loan.pk)

        if loan.status != "pending_funding":
            raise ValueError("Only loans pending funding can be funded.")
        if not schedule_confirmed:
            raise ValueError("Schedule confirmation required")
        if loan.funded_payments.filter(status__in=["processing", "completed"]).exists():
            raise ValueError("Funding already exists for this loan.")

        readiness = funding_configuration_ready(loan)
        if method == "etransfer" and not readiness["emt_configured"] and not (destination or {}).get("email"):
            raise ValueError("Funding destination required.")
        if method == "eft" and not readiness["eft_configured"] and not (destination or {}).get("bank_account_id"):
            raise ValueError("Funding destination required.")

        collections_account = collections_account or loan.collections_account or loan.bank_account
        if not collections_account:
            raise ValueError("Collections account required")

        destination_snapshot = funding_destination_snapshot(loan, method, destination)
        if not destination_snapshot or (method == "etransfer" and not destination_snapshot.get("email")):
            raise ValueError("Funding destination required")
        if method == "eft" and not destination_snapshot.get("account"):
            raise ValueError("Funding destination required")

        funding = FundedPayment.objects.create(
            loan=loan,
            amount=loan.principal,
            method=method,
            status="processing",
            destination_snapshot=destination_snapshot,
            collections_account_snapshot=account_snapshot(collections_account),
            initiated_by=user,
        )

        processor_id = ZumRailsService.initiate_transaction(
            amount=funding.amount,
            transaction_type="AccountsPayable",
            method=ZumRailsService._method(method),
            memo=f"Loan {str(loan.id)[:8]}",
            extra={"ReferenceId": str(funding.id)},
        )
        funding.processor_transaction_id = processor_id
        funding.reference = processor_id
        funding.save(update_fields=["processor_transaction_id", "reference", "updated_at"])

        loan.funding_method = method
        loan.funding_reference = destination_snapshot.get("email") or processor_id
        loan.funding_destination = destination_snapshot
        loan.collections_account = collections_account
        loan.funding_destination_locked_at = timezone.now()
        loan.collections_account_locked_at = timezone.now()
        loan.save(update_fields=[
            "funding_method",
            "funding_reference",
            "funding_destination",
            "collections_account",
            "funding_destination_locked_at",
            "collections_account_locked_at",
            "updated_at",
        ])

        loan.mark_funding_completed(
            method=method,
            reference=processor_id,
            user=user,
        )

        recommended_method = FundingMethodRecommendation.for_date()
        log_activity(
            loan,
            "system",
            "Funding Initiated",
            "Funding was sent to ZūmRails for processing.",
            created_by=getattr(user, "id", "system"),
            metadata={
                "funded_payment_id": str(funding.id),
                "method": method,
                "recommended_method": recommended_method,
                "overrode_recommendation": bool(recommended_method and recommended_method != method),
            },
        )
        return funding


class CollectionService:
    @staticmethod
    @transaction.atomic
    def initiate(loan: Loan, *, amount, user, payment: Payment | None = None):
        loan = Loan.objects.select_for_update().select_related(
            "customer",
            "collections_account",
            "bank_account",
        ).get(pk=loan.pk)

        account = loan.collections_account or loan.bank_account
        if not account:
            raise ValueError("Collections account required")

        collection = CollectionPayment.objects.create(
            loan=loan,
            payment=payment,
            amount=amount,
            status="processing",
            account_snapshot=account_snapshot(account),
            initiated_by=user,
        )
        processor_id = ZumRailsService.initiate_transaction(
            amount=collection.amount,
            transaction_type="AccountsReceivable",
            method="Eft",
            memo=f"Loan {str(loan.id)[:8]}",
            extra={"ReferenceId": str(collection.id)},
        )
        collection.processor_transaction_id = processor_id
        collection.save(update_fields=["processor_transaction_id", "updated_at"])

        log_activity(
            loan,
            "payment_scheduled",
            "Collection Initiated",
            "An EFT collection was sent to ZūmRails for processing.",
            created_by=getattr(user, "id", "system"),
            metadata={"collection_payment_id": str(collection.id)},
        )
        return collection

    @staticmethod
    @transaction.atomic
    def change_account(loan: Loan, *, new_account: BankAccount, failed_payment: CollectionPayment, user):
        loan = Loan.objects.select_for_update().select_related("collections_account", "bank_account").get(pk=loan.pk)

        if failed_payment.loan_id != loan.pk:
            raise ValueError("Failed payment not found for this loan.")
        if failed_payment.status not in ("failed", "returned", "rejected"):
            raise ValueError(
                "Collections account cannot be changed until a collection payment has failed."
            )
        if new_account.customer_id != loan.customer_id:
            raise ValueError("Collections account must belong to this customer.")

        previous = account_snapshot(loan.collections_account or loan.bank_account)
        new = account_snapshot(new_account)
        loan.collections_account = new_account
        loan.save(update_fields=["collections_account", "updated_at"])

        audit = CollectionsAccountChangeAudit.objects.create(
            loan=loan,
            previous_account=previous,
            new_account=new,
            changed_by=user,
            failed_payment=failed_payment,
            failure_reason=failed_payment.failure_reason or "",
        )
        log_activity(
            loan,
            "system",
            "Collections Account Changed",
            "Collections account was changed after a failed EFT collection.",
            created_by=getattr(user, "id", "system"),
            metadata={"audit_id": str(audit.id), "failed_payment_id": str(failed_payment.id)},
        )
        return audit


class FundingConfigurationService:
    @staticmethod
    @transaction.atomic
    def configure(
        loan: Loan,
        *,
        emt_email: str | None = None,
        emt_source: str | None = None,
        eft_bank_account_id=None,
        collections_account_id=None,
        user=None,
    ):
        loan = Loan.objects.select_for_update().select_related("customer", "customer__portal_user").get(pk=loan.pk)

        if loan.status != "pending_funding":
            raise ValueError("Only loans pending funding can be configured.")
        if loan.funding_destination_locked_at:
            raise ValueError("Funding configuration is locked.")

        destination = loan.funding_destination
        if not isinstance(destination, dict):
            destination = {}
        else:
            destination = dict(destination)

        update_fields = ["funding_destination", "updated_at"]

        if emt_email:
            destination["emt"] = {
                "email": str(emt_email),
                "source": emt_source or "application",
            }

        if eft_bank_account_id:
            try:
                account = BankAccount.objects.get(id=eft_bank_account_id, customer=loan.customer)
                loan.bank_account = account
                destination["eft"] = {
                    "bank_account_id": str(account.id),
                    "account": account_snapshot(account),
                }
                update_fields.append("bank_account")
            except BankAccount.DoesNotExist:
                raise ValueError("Selected EFT account must belong to this customer.")

        if collections_account_id:
            try:
                account = BankAccount.objects.get(id=collections_account_id, customer=loan.customer)
                loan.collections_account = account
                update_fields.append("collections_account")
            except BankAccount.DoesNotExist:
                raise ValueError("Selected collections account must belong to this customer.")

        loan.funding_destination = destination
        loan.save(update_fields=update_fields)

        log_activity(
            loan,
            "system",
            "Funding Configured",
            "Funding destination and collections account selections were saved.",
            created_by=getattr(user, "id", "system"),
        )
        return loan


class SettlementService:
    @staticmethod
    @transaction.atomic
    def complete_if_eligible(collection: CollectionPayment):
        collection = CollectionPayment.objects.select_for_update().select_related("loan", "payment").get(pk=collection.pk)
        if collection.status != "processing":
            return False
        if collection.zum_status != "Completed" or not collection.settlement_due_at:
            return False
        if collection.settlement_due_at > timezone.now():
            return False
        if any((event.get("event") in ALL_FAILURE_EVENTS) for event in (collection.event_history or [])):
            return False

        collection.status = "completed"
        collection.settled_at = timezone.now()
        collection.failure_reason = None
        collection.save(update_fields=["status", "settled_at", "failure_reason", "updated_at"])

        if collection.payment and collection.payment.status != "completed":
            collection.payment.status = "completed"
            collection.payment.processed_at = collection.settled_at
            collection.payment.reference = collection.processor_transaction_id
            collection.payment.save(update_fields=["status", "processed_at", "reference"])

        collection.loan.apply_payment(collection.amount)
        log_activity(
            collection.loan,
            "payment_completed",
            "Settlement Completed",
            "Collection settlement completed after four business days.",
            metadata={"collection_payment_id": str(collection.id)},
        )
        return True

    @staticmethod
    def process_due():
        due = CollectionPayment.objects.filter(
            status="processing",
            zum_status="Completed",
            settlement_due_at__lte=timezone.now(),
        )
        completed = 0
        for collection in due:
            if SettlementService.complete_if_eligible(collection):
                completed += 1
        return completed
