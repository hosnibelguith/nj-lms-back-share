import base64
import csv
import hashlib
import hmac
import logging
import re
import threading
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from banking.constants import (
    incomplete_bank_coordinates_message,
    normalize_bank_coordinate,
    payment_risk_warning_message,
)
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
    # Non-AutoDeposit recipient — Q&A required (see InteracHasSecurityQuestionAndAnswer).
    "InteracFailedSecurityQuestionNeededForProvidedEmail",
}

# Zum InteracSecurityAnswer: 3–25 chars, letters/digits/accents only (no spaces).
_INTERAC_ANSWER_PATTERN = re.compile(
    r"^[a-zA-Z0-9àâäèéêëîïôœùûüÿçÀÂÄÈÉÊËÎÏÔŒÙÛÜŸÇ]{3,25}$"
)
# Zum InteracSecurityQuestion: 5–40 chars, limited punctuation.
_INTERAC_QUESTION_PATTERN = re.compile(
    r"^[^\s]([a-zA-Z0-9àâäèéêëîïôœùûüÿçÀÂÄÈÉÊËÎÏÔŒÙÛÜŸÇ \-.\?!,#]+)[^\s]$"
)

ALL_FAILURE_EVENTS = EFT_FAILURE_EVENTS | INTERAC_FAILURE_EVENTS
TERMINAL_FAILURE_STATUSES = {"Returned", "Rejected"}
COLLECTION_SETTLEMENT_BUSINESS_DAYS = 4
ZUMRAILS_TOKEN_TTL_SECONDS = 55 * 60
ZUMRAILS_REQUEST_TIMEOUT_SECONDS = 15
ZUMRAILS_MEMO_PATTERN = re.compile(r"^[A-Za-z0-9 _-]{1,15}$")
# Canadian EFT AccountsReceivable batch CSV (semicolon-delimited, amounts in cents).
CANADA_AR_HEADERS = (
    "first_name*",
    "last_name*",
    "business_name",
    "institution_number*",
    "branch_number*",
    "account_number*",
    "amount_in_cents*",
    "transaction_comment",
    "memo*",
    "scheduled_date",
)


class ZumRailsError(ValueError):
    """Base error for failures communicating with Zūm Rails."""


class ZumRailsConfigurationError(ZumRailsError):
    """Raised when required Zūm Rails settings are missing."""


class ZumRailsRequestError(ZumRailsError):
    """Raised when the outcome of a Zūm Rails request is not confirmed."""

    def __init__(self, message, *, outcome_unknown=True):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def verify_zumrails_signature(payload: bytes, signature: str | None) -> bool:
    from accounts.models import GlobalSetting
    secret = GlobalSetting.get_value("ZUMRAILS_WEBHOOK_SECRET", getattr(settings, "ZUMRAILS_WEBHOOK_SECRET", ""))
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


def assert_payment_account_allowed(account: BankAccount | None, *, role: str) -> None:
    """No-op: 621/623/703 are agent-review warnings, not hard blocks.

    Call sites remain so older code paths stay stable. Warnings are surfaced via
    ``funding_configuration_ready()["warnings"]`` and the staff UI.
    """
    return None


def payment_account_risk_warning(account: BankAccount | None, *, role: str) -> str | None:
    """Return a non-blocking warning when the account is a problematic institution."""
    if account is None or not account.is_payment_blocked:
        return None
    return f"{role}: {payment_risk_warning_message(account.institution_number)}"


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


def merge_funding_destination(loan: Loan, snapshot: dict) -> dict:
    """Fold the snapshot that was actually funded into the configured destination.

    funding_configuration_ready() reads the nested ``emt``/``eft`` keys written by
    FundingConfigurationService, so replacing funding_destination with the flat
    snapshot would make an already-attempted loan report itself as unconfigured.
    """
    configured = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}
    previous_emt = configured.get("emt") if isinstance(configured.get("emt"), dict) else {}
    destination = {key: configured[key] for key in ("emt", "eft") if key in configured}
    destination.update(snapshot)

    if snapshot.get("method") == "emt" and snapshot.get("email"):
        destination["emt"] = {
            "email": snapshot["email"],
            "source": snapshot.get("source") or previous_emt.get("source") or "application",
        }
    elif snapshot.get("method") == "eft" and snapshot.get("account"):
        destination["eft"] = {
            "bank_account_id": snapshot["account"].get("id"),
            "account": snapshot["account"],
        }
    return destination


def release_funding_locks(loan: Loan) -> None:
    """Re-open destination selection after a funding attempt definitively failed.

    The locks stop a funded loan from having its destination rewritten. A failed
    attempt moved no money, so staff must be able to correct the accounts and retry.
    """
    if not loan.funding_destination_locked_at and not loan.collections_account_locked_at:
        return
    loan.funding_destination_locked_at = None
    loan.collections_account_locked_at = None
    loan.save(update_fields=[
        "funding_destination_locked_at",
        "collections_account_locked_at",
        "updated_at",
    ])


def has_active_funding_attempt(loan: Loan) -> bool:
    return loan.funded_payments.filter(status__in=["processing", "completed"]).exists()


def get_active_funding_attempt(loan: Loan) -> FundedPayment | None:
    return (
        loan.funded_payments.filter(status__in=["processing", "completed"])
        .order_by("-created_at")
        .first()
    )


def is_unsubmitted_funding_attempt(funding: FundedPayment) -> bool:
    """True when an attempt is still `processing` but Zūm never assigned a tx id."""
    return (
        funding.status == "processing"
        and not (funding.processor_transaction_id or "").strip()
    )


def extract_zum_transaction_fields(payload) -> dict:
    """Pull status/reason from a Zum webhook payload or GET /transaction response."""
    data = payload if isinstance(payload, dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    source = result or data
    nested = source.get("Transaction") if isinstance(source.get("Transaction"), dict) else {}
    if not nested and isinstance(data.get("Transaction"), dict):
        nested = data["Transaction"]

    status_value = (
        source.get("TransactionStatus")
        or source.get("Status")
        or nested.get("TransactionStatus")
        or nested.get("Status")
        or data.get("TransactionStatus")
        or data.get("Status")
    )
    reason = (
        source.get("FailedTransactionEvent")
        or nested.get("FailedTransactionEvent")
        or data.get("FailedTransactionEvent")
        or source.get("Event")
        or nested.get("Event")
        or data.get("Event")
        or source.get("MemberMessage")
        or nested.get("MemberMessage")
        or data.get("MemberMessage")
    )
    history_failure = None
    for blob in (source, nested, data):
        if not isinstance(blob, dict):
            continue
        history = blob.get("TransactionHistory")
        if not isinstance(history, list):
            continue
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            event = item.get("Event") or item.get("event")
            if isinstance(event, str) and (
                event in ALL_FAILURE_EVENTS
                or event.startswith(("EftFailed", "InteracFailed"))
            ):
                history_failure = event
                break
        if history_failure:
            break
    if isinstance(reason, str):
        reason = reason.strip() or None
    if isinstance(status_value, str):
        status_value = status_value.strip() or None
    status_is_failure = isinstance(status_value, str) and (
        "Failed" in status_value or status_value in TERMINAL_FAILURE_STATUSES
    )
    reason_is_failure = isinstance(reason, str) and (
        reason in ALL_FAILURE_EVENTS
        or reason.startswith(("EftFailed", "InteracFailed"))
    )
    # NSF/return can land after Succeeded. Prefer the failure over a leftover Completed status.
    if not status_is_failure and (history_failure or reason_is_failure):
        status_value = "Failed"
        if not reason_is_failure:
            reason = history_failure
    return {
        "status": status_value,
        "reason": reason,
        "transaction_id": (
            source.get("Id")
            or source.get("TransactionId")
            or nested.get("Id")
            or data.get("Id")
            or data.get("TransactionId")
        ),
    }


def is_zum_failed_status(status_value) -> bool:
    return isinstance(status_value, str) and "Failed" in status_value


def collection_failure_status_from_zum(status_value: str | None) -> str:
    normalized = normalize_zum_status(status_value)
    if normalized == "Returned":
        return "returned"
    if normalized == "Rejected":
        return "rejected"
    return "failed"


def is_terminal_zum_collection_failure(status_value=None, reason=None) -> bool:
    """True when Zūm has already failed/returned/rejected an AR collection."""
    if is_zum_failed_status(status_value):
        return True
    if normalize_zum_status(status_value) in TERMINAL_FAILURE_STATUSES:
        return True
    reason_text = reason.strip() if isinstance(reason, str) else ""
    if not reason_text:
        return False
    if reason_text in ALL_FAILURE_EVENTS:
        return True
    compact = re.sub(r"[\s_\-]+", "", reason_text)
    return (
        reason_text.startswith(("EftFailed", "InteracFailed"))
        or compact.startswith(("EftFailed", "InteracFailed"))
    )


def normalize_zum_status(status_value: str | None) -> str | None:
    """Normalize Zum status spelling so Cancelled/Canceled both close the attempt."""
    if not status_value or not isinstance(status_value, str):
        return None
    value = status_value.strip()
    if not value:
        return None
    aliases = {
        "canceled": "Cancelled",
        "cancelled": "Cancelled",
        "returned": "Returned",
        "rejected": "Rejected",
        "completed": "Completed",
        "failed": "Failed",
        "inprogress": "InProgress",
        "in progress": "InProgress",
        "pending cancellation": "Pending Cancellation",
        "pendingcancellation": "Pending Cancellation",
    }
    return aliases.get(value.lower(), value)


def is_zum_cancelled_status(status_value) -> bool:
    return normalize_zum_status(status_value) == "Cancelled"


def is_terminal_zum_funding_status(status_value) -> bool:
    """Statuses that must close the local attempt and allow Fund Customer again."""
    normalized = normalize_zum_status(status_value)
    if not normalized:
        return False
    return (
        normalized in ("Returned", "Rejected", "Cancelled")
        or is_zum_failed_status(normalized)
    )


def reason_implies_interac_security_failure(reason: str | None) -> bool:
    """True for Zum MemberMessage / event text that requires Interac Q&A."""
    text = (reason or "").strip().lower()
    if not text:
        return False
    return "security question" in text and (
        "interac" in text or "needed" in text or "not authorized" in text
    )


def apply_funded_payment_zum_status(
    funding: FundedPayment,
    *,
    status_value: str | None,
    reason: str | None = None,
) -> FundedPayment:
    """Apply a Zum transaction status to funding; unlocks retry on failed/returned/cancelled."""
    status_value = normalize_zum_status(status_value)
    reason = (reason or "").strip() or None
    display_reason = reason or status_value or "Failed"

    # Human-readable Interac Q&A failures sometimes arrive while status is still InProgress.
    if (
        reason_implies_interac_security_failure(reason)
        and status_value not in ("Completed", "Returned", "Cancelled")
        and not is_zum_failed_status(status_value or "")
    ):
        status_value = "Failed"

    if status_value == "Completed":
        funding.zum_status = status_value
        # Stale Completed after a terminal failure/return must not revive the attempt
        # (Zum can replay Completed after Returned / Interac failure).
        if funding.status in ("returned", "failed", "cancelled"):
            funding.save(update_fields=["zum_status", "updated_at"])
            return funding

        was_incomplete = funding.status != "completed"
        loan_was_inactive = funding.loan.status != "active"

        if funding.status != "completed":
            # Heal processing rows (and complete-on-create already-completed is idempotent).
            funding.failure_reason = None
            funding.status = "completed"
            if not funding.completed_at:
                funding.completed_at = timezone.now()
            funding.save(update_fields=[
                "status",
                "zum_status",
                "failure_reason",
                "completed_at",
                "updated_at",
            ])
        else:
            funding.save(update_fields=["zum_status", "updated_at"])

        if funding.loan.status != "active":
            funding.loan.mark_funding_completed(
                method=funding.method,
                reference=funding.processor_transaction_id or "",
            )
        if was_incomplete or loan_was_inactive:
            log_activity(
                funding.loan,
                "loan_funded",
                "Funding Completed",
                "ZūmRails confirmed funding completion.",
                metadata={"funded_payment_id": str(funding.id)},
            )
        from activity.services import resolve_funding_failure_alerts

        resolve_funding_failure_alerts(funding.loan, reason="funding_completed")
        if was_incomplete or loan_was_inactive:
            FundingService._queue_funding_email(funding.loan)
        return funding

    if status_value == "Returned":
        was_returned = funding.status == "returned"
        funding.zum_status = status_value
        if not was_returned:
            funding.mark_returned(display_reason)
            _reopen_loan_after_funding_failure_safe(funding)
            log_activity(
                funding.loan,
                "system",
                "Funding Returned",
                display_reason,
                metadata={
                    "funded_payment_id": str(funding.id),
                    "staff_alert": True,
                    "alert_kind": "funding_failure",
                    "failure_reason": display_reason,
                    "is_resolved": False,
                },
            )
        else:
            funding.save(update_fields=["zum_status", "updated_at"])
        return funding

    if status_value == "Cancelled":
        # Terminal at Zum — no money moving; unlock so staff can fund again.
        was_cancelled = funding.status == "cancelled"
        funding.zum_status = status_value
        funding.failure_reason = display_reason
        if not was_cancelled:
            funding.status = "cancelled"
            funding.save(update_fields=[
                "status",
                "zum_status",
                "failure_reason",
                "updated_at",
            ])
            _reopen_loan_after_funding_failure_safe(funding)
            log_activity(
                funding.loan,
                "system",
                "Funding Cancelled",
                display_reason,
                metadata={
                    "funded_payment_id": str(funding.id),
                    "staff_alert": True,
                    "alert_kind": "funding_failure",
                    "failure_reason": display_reason,
                    "is_resolved": False,
                },
            )
        else:
            funding.save(update_fields=["zum_status", "failure_reason", "updated_at"])
        return funding

    if status_value and (is_zum_failed_status(status_value) or status_value == "Rejected"):
        was_failed = funding.status == "failed"
        funding.zum_status = status_value
        if not was_failed:
            funding.mark_failed(display_reason)
            _reopen_loan_after_funding_failure_safe(funding)
            log_activity(
                funding.loan,
                "system",
                "Funding Failed",
                display_reason,
                metadata={
                    "funded_payment_id": str(funding.id),
                    "staff_alert": True,
                    "alert_kind": "funding_failure",
                    "failure_reason": display_reason,
                    "is_resolved": False,
                },
            )
        else:
            funding.failure_reason = display_reason
            funding.save(update_fields=["zum_status", "failure_reason", "updated_at"])
        return funding

    # Still in flight (InProgress / Pending Cancellation / Scheduled / InReview / etc.)
    update_fields = ["updated_at"]
    if status_value:
        funding.zum_status = status_value
        update_fields.append("zum_status")
    if reason and reason not in ("Updated", "Created", status_value):
        funding.failure_reason = reason
        update_fields.append("failure_reason")
    funding.save(update_fields=list(dict.fromkeys(update_fields)))
    return funding


def _reopen_loan_after_funding_failure_safe(funding: FundedPayment) -> None:
    """Local reopen helper (mirrors webhook) so sync can unlock without circular imports."""
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


def active_funding_block_message(loan: Loan) -> str | None:
    """Human-readable funding block including Zūm failure/status when available."""
    attempt = get_active_funding_attempt(loan)
    if not attempt:
        return None
    if attempt.status == "completed":
        return "Funding already exists for this loan."

    zum_reason = (attempt.failure_reason or "").strip()
    zum_status = (attempt.zum_status or "").strip()
    tx_id = (attempt.processor_transaction_id or "").strip()

    if is_unsubmitted_funding_attempt(attempt):
        if zum_reason:
            return (
                f"Previous funding attempt did not complete at Zūm: {zum_reason}. "
                "Release the stuck attempt to fund again."
            )
        return (
            "A funding attempt is stuck before Zūm accepted it. "
            "Release the stuck attempt to fund again."
        )
    if zum_reason:
        return f"Funding already exists for this loan. Zūm reason: {zum_reason}."
    if zum_status:
        message = f"Funding already exists for this loan. Zūm status: {zum_status}"
        if tx_id:
            message += f" (transaction {tx_id})"
        return f"{message}."
    if tx_id:
        return (
            f"Funding already exists for this loan. "
            f"Zūm transaction {tx_id} is still processing."
        )
    return "Funding already exists for this loan."


def assert_funding_configuration_editable(loan: Loan) -> None:
    """Locks only apply while money may be moving.

    A failed/returned attempt (or a leftover lock from an older code path) must
    not permanently freeze destination selection — that is what the staff UI
    already assumes when it keys off funded-payment status.
    """
    if not loan.funding_destination_locked_at:
        return
    if has_active_funding_attempt(loan):
        raise ValueError("Funding configuration is locked.")
    release_funding_locks(loan)


def is_arrive_funded_loan(loan: Loan) -> bool:
    """Arrive funds the card after our funding webhook — never LendStack EFT/EMT."""
    from accounts.models import Customer

    customer = getattr(loan, "customer", None)
    return bool(customer and getattr(customer, "source", None) == Customer.SOURCE_ARRIVE)


def funding_configuration_ready(loan: Loan) -> dict:
    configured = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}
    emt = configured.get("emt") if isinstance(configured.get("emt"), dict) else {}
    eft = configured.get("eft") if isinstance(configured.get("eft"), dict) else {}
    collections_account = loan.collections_account or loan.bank_account
    emt_configured = bool(emt.get("email"))
    eft_configured = bool(eft.get("account") or loan.bank_account)
    collections_configured = bool(collections_account)
    blockers = []
    warnings = []
    arrive_loan = is_arrive_funded_loan(loan)
    active_funding = get_active_funding_attempt(loan)
    if loan.status != "pending_funding":
        blockers.append("Loan is not pending funding.")
    if not loan.contract_signed:
        blockers.append("Contract must be signed before funding.")
    funding_block = active_funding_block_message(loan)
    if funding_block:
        blockers.append(funding_block)
    if not arrive_loan:
        # A loan is funded by EFT or by e-Transfer, never both, so only one
        # destination is needed here. initiate() enforces the chosen method.
        if not emt_configured and not eft_configured:
            blockers.append("Funding destination required.")
        if not collections_configured:
            blockers.append("Collections account required.")
    elif not collections_configured:
        # Arrive funds via Card Issuance, but staff must still pick the
        # collections (repayment) bank account when multiple chequing accounts exist.
        blockers.append("Collections account required.")

    # Full EFT coordinates required before any disbursement / collections setup.
    if collections_account:
        incomplete = incomplete_bank_coordinates_message(
            collections_account, role="Collections account"
        )
        if incomplete:
            blockers.append(incomplete)

    # Problematic banks (621/623/703): warn only — agents may still proceed.
    warning = payment_account_risk_warning(
        collections_account, role="Collections account"
    )
    if warning:
        warnings.append(warning)
    eft_account = None
    if not arrive_loan:
        eft_account = loan.bank_account
        eft_account_id = eft.get("bank_account_id") or (eft.get("account") or {}).get("id")
        if eft_account_id:
            eft_account = (
                BankAccount.objects.filter(id=eft_account_id, customer_id=loan.customer_id).first()
                or eft_account
            )
        if eft_configured and eft_account:
            incomplete = incomplete_bank_coordinates_message(
                eft_account, role="Funding account"
            )
            if incomplete:
                blockers.append(incomplete)
        warning = payment_account_risk_warning(eft_account, role="Funding account")
        if warning:
            warnings.append(warning)

    active_reason = None
    if active_funding:
        active_reason = (
            (active_funding.failure_reason or active_funding.zum_status or "").strip()
            or None
        )
    return {
        "emt_configured": emt_configured,
        "eft_configured": eft_configured,
        "collections_account_configured": collections_configured,
        "has_active_funding": bool(active_funding),
        "can_release_stuck_funding": bool(
            active_funding and is_unsubmitted_funding_attempt(active_funding)
        ),
        "active_funding_status": active_funding.status if active_funding else None,
        "active_funding_failure_reason": active_reason,
        "arrive_external_funding": arrive_loan,
        "recommended_method_override": "card_issuance" if arrive_loan else None,
        "allowed_methods": (
            ["card_issuance"] if arrive_loan else ["eft", "etransfer"]
        ),
        "blockers": blockers,
        "warnings": warnings,
    }


def apply_collection_failure(collection: CollectionPayment, *, reason: str, status: str = "failed"):
    was_settled = collection.status == "completed"
    collection.status = status
    collection.failure_reason = reason
    update_fields = ["status", "failure_reason", "updated_at"]
    if collection.returned_at is None:
        collection.returned_at = timezone.now()
        update_fields.append("returned_at")
    collection.save(update_fields=update_fields)

    payment = collection.payment
    if payment and payment.status not in ("failed", "nsf", "cancelled"):
        if "InsufficientFunds" in reason:
            payment.mark_nsf()
        else:
            payment.fail(reason)

    if was_settled:
        loan = collection.loan
        loan.balance += collection.amount
        update_fields = ["balance", "updated_at"]
        if loan.status == "paid_off":
            loan.status = "active"
            loan.is_active = True
            update_fields.extend(["status", "is_active"])
        loan.save(update_fields=update_fields)

    from .services import LoanService

    fee_payment = LoanService.apply_collection_failure_fee(collection, reason=reason)
    log_activity(
        collection.loan,
        "payment_failed",
        "Collection Failure Fee Added",
        (
            f"Zūm collection failed: {reason or 'Unknown reason'}. "
            "$50 collection failure fee and missed-payment recovery were appended after the schedule."
        ),
        metadata={
            "collection_payment_id": str(collection.id),
            "fee_payment_id": str(fee_payment.id) if fee_payment else None,
            "failure_reason": reason,
            "staff_alert": True,
            "alert_kind": "collection_failure",
            "is_resolved": False,
        },
    )


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
    _authentication_lock = threading.Lock()

    @staticmethod
    def _method(method: str) -> str:
        return "Interac" if method == "etransfer" else "Eft"

    @classmethod
    def _setting(cls, key: str, default=""):
        from accounts.models import GlobalSetting

        value = GlobalSetting.get_value(key, getattr(settings, key, default))
        return value if value is not None else default

    @classmethod
    def _is_dry_run(cls) -> bool:
        return str(cls._setting("ZUMRAILS_DRY_RUN", False)).lower() == "true"

    @classmethod
    def _base_url(cls) -> str:
        base_url = str(cls._setting("ZUMRAILS_API_BASE_URL", "")).rstrip("/")
        if not base_url:
            raise ZumRailsConfigurationError("ZūmRails API base URL is not configured.")
        return base_url

    @classmethod
    def _endpoint(cls, path: str) -> str:
        base_url = cls._base_url()
        normalized_path = "/" + path.lstrip("/")
        if base_url.lower().endswith("/api"):
            return f"{base_url}{normalized_path}"
        return f"{base_url}/api{normalized_path}"

    @classmethod
    def _credentials(cls):
        username = str(cls._setting("ZUMRAILS_USERNAME", "")).strip()
        password = str(cls._setting("ZUMRAILS_PASSWORD", ""))
        if not username or not password:
            raise ZumRailsConfigurationError(
                "ZūmRails API username and password are not configured."
            )
        return username, password

    @classmethod
    def _token_cache_key(cls) -> str:
        username, _ = cls._credentials()
        digest = hashlib.sha256(f"{cls._base_url()}:{username}".encode("utf-8")).hexdigest()
        return f"zumrails:token:{digest}"

    @staticmethod
    def _result(data):
        if isinstance(data, dict) and isinstance(data.get("result"), (dict, list)):
            return data["result"]
        return data

    @classmethod
    def _authenticate(cls, *, force=False) -> str:
        cache_key = cls._token_cache_key()
        if not force:
            cached_token = cache.get(cache_key)
            if cached_token:
                return cached_token

        with cls._authentication_lock:
            if not force:
                cached_token = cache.get(cache_key)
                if cached_token:
                    return cached_token

            username, password = cls._credentials()
            try:
                response = requests.post(
                    cls._endpoint("/authorize"),
                    json={"Username": username, "Password": password},
                    headers={"Content-Type": "application/json"},
                    timeout=ZUMRAILS_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = cls._result(response.json())
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                detail = f" HTTP {status_code}." if status_code else ""
                raise ZumRailsRequestError(
                    f"Unable to authenticate with ZūmRails.{detail} "
                    "Check Zūm API base URL, username, and password in API Integrations.",
                    outcome_unknown=False,
                ) from exc
            except (requests.RequestException, ValueError) as exc:
                raise ZumRailsRequestError(
                    "Unable to authenticate with ZūmRails. "
                    "Check Zūm API base URL, username, and password in API Integrations.",
                    outcome_unknown=False,
                ) from exc

            token = data.get("Token") if isinstance(data, dict) else None
            token = token or (data.get("token") if isinstance(data, dict) else None)
            if not token:
                raise ZumRailsRequestError(
                    "ZūmRails authentication did not return a bearer token.",
                    outcome_unknown=False,
                )
            cache.set(cache_key, token, ZUMRAILS_TOKEN_TTL_SECONDS)
            return token

    @classmethod
    def _request(cls, method: str, path: str, *, json_payload=None, headers=None):
        token = cls._authenticate()
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        request_headers.update(headers or {})

        for attempt in range(2):
            try:
                response = requests.request(
                    method,
                    cls._endpoint(path),
                    json=json_payload,
                    headers=request_headers,
                    timeout=ZUMRAILS_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise ZumRailsRequestError(
                    "ZūmRails request outcome is unknown; the existing attempt was retained."
                ) from exc

            if response.status_code == 401 and attempt == 0:
                cache.delete(cls._token_cache_key())
                request_headers["Authorization"] = f"Bearer {cls._authenticate(force=True)}"
                continue

            try:
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as exc:
                raise ZumRailsRequestError(
                    f"ZūmRails rejected the request with HTTP {response.status_code}.",
                    outcome_unknown=(
                        response.status_code >= 500
                        or response.status_code in (408, 409, 429)
                    ),
                ) from exc
            except ValueError as exc:
                raise ZumRailsRequestError(
                    "ZūmRails returned an invalid JSON response."
                ) from exc

            if isinstance(data, dict) and data.get("isError") is True:
                message = (
                    data.get("responseException")
                    or data.get("message")
                    or "request failed"
                )
                raise ZumRailsRequestError(
                    f"ZūmRails rejected the request: {message}",
                    outcome_unknown=False,
                )
            return data

        raise ZumRailsRequestError(
            "ZūmRails authentication failed.",
            outcome_unknown=False,
        )

    @classmethod
    def get_wallet_id(cls) -> str:
        configured_wallet_id = str(cls._setting("ZUMRAILS_WALLET_ID", "")).strip()
        if configured_wallet_id:
            return configured_wallet_id

        username, _ = cls._credentials()
        wallet_scope = f"{cls._base_url()}:{username}".encode("utf-8")
        cache_key = f"zumrails:wallet:{hashlib.sha256(wallet_scope).hexdigest()}"
        cached_wallet_id = cache.get(cache_key)
        if cached_wallet_id:
            return cached_wallet_id

        try:
            data = cls._result(cls._request("GET", "/wallet"))
        except ZumRailsRequestError as exc:
            raise ZumRailsRequestError(
                str(exc),
                outcome_unknown=False,
            ) from exc
        wallets = data if isinstance(data, list) else []
        cad_wallets = [
            wallet for wallet in wallets
            if isinstance(wallet, dict) and str(wallet.get("Currency", "CAD")).upper() == "CAD"
        ]
        selected = cad_wallets[0] if cad_wallets else (wallets[0] if wallets else None)
        wallet_id = selected.get("Id") if isinstance(selected, dict) else None
        if not wallet_id:
            raise ZumRailsConfigurationError("No ZūmRails wallet is available.")
        cache.set(cache_key, wallet_id, ZUMRAILS_TOKEN_TTL_SECONDS)
        return wallet_id

    @classmethod
    def _funds_destination(cls) -> dict:
        """Exactly one of WalletId or FundingSourceId for single AR/AP transactions."""
        wallet_id = str(cls._setting("ZUMRAILS_WALLET_ID", "")).strip()
        if wallet_id:
            return {"WalletId": wallet_id}
        funding_source_id = str(cls._setting("ZUMRAILS_FUNDING_SOURCE_ID", "")).strip()
        if funding_source_id:
            return {"FundingSourceId": funding_source_id}
        return {"WalletId": cls.get_wallet_id()}

    @staticmethod
    def _money(amount) -> float:
        return float(
            Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    @staticmethod
    def _cents(amount) -> int:
        return int(
            (Decimal(str(amount)) * Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    @staticmethod
    def _memo(value: str) -> str:
        memo = re.sub(r"[^A-Za-z0-9 _-]", "", str(value or ""))[:15].strip()
        if not ZUMRAILS_MEMO_PATTERN.fullmatch(memo):
            raise ZumRailsConfigurationError("ZūmRails memo is invalid.")
        return memo

    @classmethod
    def interac_security_payload(cls, *, user_payload: dict | None = None) -> dict:
        """Build Interac Q&A fields for AccountsPayable.

        Zum rejects non-AutoDeposit emails when InteracHasSecurityQuestionAndAnswer
        is false ("Security Question Needed For Provided Email"). Always send one
        shared company question/answer (API Integrations / env; defaults Mohawk/Loans).
        """
        # user_payload kept for call-site compatibility; Q&A is company-wide.
        question = str(
            cls._setting("ZUMRAILS_INTERAC_SECURITY_QUESTION", "Mohawk")
        ).strip() or "Mohawk"
        answer = str(
            cls._setting("ZUMRAILS_INTERAC_SECURITY_ANSWER", "Loans")
        ).strip() or "Loans"

        if not _INTERAC_QUESTION_PATTERN.fullmatch(question):
            question = "Mohawk"
        if not _INTERAC_ANSWER_PATTERN.fullmatch(answer):
            answer = "Loans"

        return {
            "InteracNotificationChannel": "email",
            "InteracHasSecurityQuestionAndAnswer": True,
            "InteracSecurityQuestion": question[:40],
            "InteracSecurityAnswer": answer[:25],
        }

    @staticmethod
    def user_payload(customer, *, account: BankAccount | None = None, email=None) -> dict:
        phone = re.sub(r"\D", "", customer.phone or "")[-10:]
        payload = {
            "FirstName": customer.first_name,
            "LastName": customer.last_name,
            "Email": email or customer.email,
            "ClientUserId": str(customer.id),
        }
        if phone:
            payload["PhoneNumber"] = phone
        if account:
            assert_payment_account_allowed(account, role="Selected bank account")
            incomplete = incomplete_bank_coordinates_message(
                account, role="Selected EFT account"
            )
            if incomplete:
                raise ZumRailsConfigurationError(incomplete)
            institution = normalize_bank_coordinate(account.institution_number)
            transit = normalize_bank_coordinate(account.transit_number)
            account_number = normalize_bank_coordinate(account.account_number)
            # Zūm requires zero-padded institution (3) and transit/branch (5).
            payload["BankAccountInformation"] = {
                "InstitutionNumber": str(institution).zfill(3),
                "TransitNumber": str(transit).zfill(5),
                "AccountNumber": str(account_number),
            }
        return payload

    @classmethod
    def initiate_transaction(
        cls,
        *,
        amount,
        transaction_type: str,
        method: str,
        memo: str,
        client_transaction_id,
        user_payload: dict,
        comment="",
    ) -> str:
        dry_run = cls._is_dry_run()
        if dry_run:
            return f"dryrun-{transaction_type.lower()}-{uuid.uuid4().hex}"

        payload = {
            "ZumRailsType": transaction_type,
            "TransactionMethod": method,
            "Amount": cls._money(amount),
            "Memo": cls._memo(memo),
            "ClientTransactionId": str(client_transaction_id),
            "User": user_payload,
            **cls._funds_destination(),
        }
        if comment:
            payload["Comment"] = comment
        if method == "Interac":
            payload.update(cls.interac_security_payload(user_payload=user_payload))

        response_data = cls._result(
            cls._request(
                "POST",
                "/transaction",
                json_payload=payload,
                headers={"idempotency-key": str(client_transaction_id)},
            )
        )
        transaction_id = (
            response_data.get("Id")
            or response_data.get("TransactionId")
            or response_data.get("id")
            if isinstance(response_data, dict)
            else None
        )
        if not transaction_id:
            raise ZumRailsRequestError("ZūmRails did not return a transaction ID.")
        return transaction_id

    @classmethod
    def get_transaction(cls, transaction_id: str) -> dict:
        """GET /api/transaction/{id} — used to refresh status/reason when webhooks lag."""
        tx_id = (transaction_id or "").strip()
        if not tx_id:
            raise ZumRailsConfigurationError("Zūm transaction id is required.")
        if cls._is_dry_run():
            return {
                "Id": tx_id,
                "TransactionStatus": "InProgress",
                "FailedTransactionEvent": None,
            }
        data = cls._result(cls._request("GET", f"/transaction/{tx_id}"))
        if not isinstance(data, dict):
            raise ZumRailsRequestError(
                "ZūmRails returned an unexpected transaction payload.",
                outcome_unknown=False,
            )
        return data

    @classmethod
    def _batch_destination(cls, destination: str | None = None) -> dict:
        if destination == "funding":
            funding_source_id = str(cls._setting("ZUMRAILS_FUNDING_SOURCE_ID", "")).strip()
            if not funding_source_id:
                raise ZumRailsConfigurationError(
                    "ZUMRAILS_FUNDING_SOURCE_ID is required for a funding-source destination."
                )
            return {"FundingSourceId": funding_source_id}
        if destination == "wallet":
            return {"WalletId": cls.get_wallet_id()}
        return cls._funds_destination()

    @classmethod
    def build_accounts_receivable_csv(cls, rows) -> str:
        """Build Zūm Canadian EFT AR batch CSV (semicolon-delimited, cents)."""
        buffer = StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=CANADA_AR_HEADERS,
            delimiter=";",
            lineterminator="\n",
            extrasaction="raise",
        )
        writer.writeheader()
        for source in rows:
            first_name = str(source.get("first_name", "")).strip()
            last_name = str(source.get("last_name", "")).strip()
            business_name = str(source.get("business_name", "")).strip()
            if not business_name and not (first_name and last_name):
                raise ZumRailsConfigurationError(
                    "Each AR batch row needs first+last name or business_name."
                )
            writer.writerow({
                "first_name*": first_name,
                "last_name*": last_name,
                "business_name": business_name,
                "institution_number*": str(source["institution_number"]).strip().zfill(3),
                "branch_number*": str(source["transit_number"]).strip().zfill(5),
                "account_number*": str(source["account_number"]).strip(),
                "amount_in_cents*": cls._cents(source["amount"]),
                "transaction_comment": str(source.get("comment", "")),
                "memo*": cls._memo(str(source["memo"])),
                "scheduled_date": str(source.get("scheduled_date", "")),
            })
        return buffer.getvalue()

    @classmethod
    def _ar_batch_base_payload(cls, csv_bytes: bytes, *, destination: str | None = None) -> dict:
        return {
            "TransactionType": "AccountsReceivable",
            "Bytes": base64.b64encode(csv_bytes).decode("ascii"),
            **cls._batch_destination(destination),
        }

    @classmethod
    def _validate_ar_batch_payload(cls, payload: dict) -> dict:
        response = cls._request("POST", "/transaction/ValidateBatchFile", json_payload=payload)
        result = cls._result(response)
        if not isinstance(result, dict):
            raise ZumRailsRequestError(
                "ZūmRails batch validation returned an unexpected result.",
                outcome_unknown=False,
            )
        invalid = int(result.get("InvalidTransactions", 0) or 0)
        status = str(result.get("Status", ""))
        if invalid or status.lower() != "ok":
            raise ZumRailsRequestError(
                f"ZūmRails batch validation failed: status={status!r}, invalid={invalid}.",
                outcome_unknown=False,
            )
        return response

    @classmethod
    def validate_accounts_receivable_batch(
        cls,
        csv_content: str | bytes,
        *,
        destination: str | None = None,
    ) -> dict:
        """Validate a Canadian EFT AR CSV without processing (ValidateBatchFile)."""
        csv_bytes = (
            csv_content.encode("utf-8") if isinstance(csv_content, str) else csv_content
        )
        return cls._validate_ar_batch_payload(
            cls._ar_batch_base_payload(csv_bytes, destination=destination)
        )

    @classmethod
    def process_accounts_receivable_batch(
        cls,
        csv_content: str | bytes,
        *,
        idempotency_key: str,
        filename: str = "accounts_receivable.csv",
        destination: str | None = None,
    ) -> dict:
        """Validate then process a Canadian EFT AR batch (ProcessBatchFile).

        Scheduled collections continue to use initiate_transaction so webhooks can
        match ClientTransactionId. Call this only for intentional batch ops.
        """
        if cls._is_dry_run():
            return {
                "result": {
                    "Id": f"dryrun-batch-{uuid.uuid4().hex}",
                    "Status": "Ok",
                }
            }

        csv_bytes = (
            csv_content.encode("utf-8") if isinstance(csv_content, str) else csv_content
        )
        base_payload = cls._ar_batch_base_payload(csv_bytes, destination=destination)
        # Validate and process the exact same Base64 bytes.
        cls._validate_ar_batch_payload(base_payload)
        payload = {
            **base_payload,
            "FileName": filename,
            "SkipFileAlreadyProcessedInLast24Hours": True,
            "WithdrawSumTotalFromFundingSource": False,
            "TransactionMethod": "Eft",
        }
        return cls._request(
            "POST",
            "/transaction/ProcessBatchFile",
            json_payload=payload,
            headers={"idempotency-key": str(idempotency_key)[:36]},
        )


class FundingService:
    @staticmethod
    def initiate(loan: Loan, *, method: str, schedule_confirmed: bool, user, destination=None, collections_account=None):
        try:
            with transaction.atomic():
                loan = Loan.objects.select_for_update().select_related("customer").get(pk=loan.pk)
                arrive_loan = is_arrive_funded_loan(loan)

                if arrive_loan and method in ("eft", "etransfer"):
                    raise ValueError(
                        "Arrive loans cannot be funded via EFT / e-Transfer. Use Card Issuance."
                    )
                if not arrive_loan and method == "card_issuance":
                    raise ValueError(
                        "Card Issuance is only available for Arrive applications. Use EFT or e-Transfer."
                    )
                if loan.status != "pending_funding":
                    raise ValueError("Only loans pending funding can be funded.")
                if not loan.contract_signed:
                    raise ValueError("Contract must be signed before funding.")
                if not schedule_confirmed:
                    raise ValueError("Schedule confirmation required")
                funding_block = active_funding_block_message(loan)
                if funding_block:
                    raise ValueError(funding_block)
                # Clear leftover locks from a prior failed attempt so destination
                # edits and retries stay consistent with funded-payment status.
                assert_funding_configuration_editable(loan)

                readiness = funding_configuration_ready(loan)
                if (
                    method == "etransfer"
                    and not readiness["emt_configured"]
                    and not (destination or {}).get("email")
                ):
                    raise ValueError("Funding destination required.")
                if (
                    method == "eft"
                    and not readiness["eft_configured"]
                    and not (destination or {}).get("bank_account_id")
                ):
                    raise ValueError("Funding destination required.")

                collections_account = (
                    collections_account or loan.collections_account or loan.bank_account
                )
                if method in ("eft", "etransfer", "card_issuance") and not collections_account:
                    raise ValueError("Collections account required")
                assert_payment_account_allowed(collections_account, role="Collections account")
                incomplete_collections = incomplete_bank_coordinates_message(
                    collections_account, role="Collections account"
                )
                if incomplete_collections:
                    raise ValueError(incomplete_collections)

                if method == "card_issuance":
                    return FundingService._complete_card_issuance(
                        loan, collections_account=collections_account, user=user
                    )

                destination_snapshot = funding_destination_snapshot(loan, method, destination)
                if (
                    not destination_snapshot
                    or (
                        method == "etransfer"
                        and not destination_snapshot.get("email")
                    )
                ):
                    raise ValueError("Funding destination required")
                if method == "eft":
                    if not destination_snapshot.get("account"):
                        raise ValueError("Funding destination required")
                    funding_account = BankAccount.objects.filter(
                        id=destination_snapshot["account"].get("id"),
                        customer=loan.customer,
                    ).first()
                    assert_payment_account_allowed(
                        funding_account,
                        role="Funding account",
                    )
                    incomplete_funding = incomplete_bank_coordinates_message(
                        funding_account, role="Funding account"
                    )
                    if incomplete_funding:
                        raise ValueError(incomplete_funding)

                funding = FundedPayment.objects.create(
                    loan=loan,
                    amount=loan.principal,
                    method=method,
                    status="processing",
                    destination_snapshot=destination_snapshot,
                    collections_account_snapshot=account_snapshot(collections_account),
                    initiated_by=user,
                )

                loan.funding_method = method
                loan.funding_reference = destination_snapshot.get("email") or str(funding.id)
                loan.funding_destination = merge_funding_destination(loan, destination_snapshot)
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
        except IntegrityError as exc:
            raise ValueError("Funding already exists for this loan.") from exc

        try:
            if method == "eft":
                destination_account_id = destination_snapshot["account"].get("id")
                destination_account = BankAccount.objects.filter(
                    id=destination_account_id,
                    customer=loan.customer,
                ).first()
                if not destination_account:
                    raise ZumRailsConfigurationError("Funding destination required.")
                zum_user = ZumRailsService.user_payload(
                    loan.customer,
                    account=destination_account,
                )
            else:
                zum_user = ZumRailsService.user_payload(
                    loan.customer,
                    email=destination_snapshot["email"],
                )

            processor_id = ZumRailsService.initiate_transaction(
                amount=funding.amount,
                transaction_type="AccountsPayable",
                method=ZumRailsService._method(method),
                memo=f"Loan {str(loan.id)[:8]}",
                comment=f"Loan disbursement {str(loan.id)[:8]}",
                client_transaction_id=funding.id,
                user_payload=zum_user,
            )
        except ZumRailsError as exc:
            # Only an unknown outcome may have moved money. Anything else never
            # reached Zūm, so close the attempt and unlock the destination for retry.
            if isinstance(exc, ZumRailsRequestError) and exc.outcome_unknown:
                funding.failure_reason = str(exc)
                funding.save(update_fields=["failure_reason", "updated_at"])
            else:
                funding.status = "failed"
                funding.failure_reason = str(exc)
                release_funding_locks(loan)
                funding.save(update_fields=["status", "failure_reason", "updated_at"])
                log_activity(
                    loan,
                    "system",
                    "Funding Failed",
                    str(exc),
                    created_by=getattr(user, "id", "system"),
                    metadata={
                        "funded_payment_id": str(funding.id),
                        "staff_alert": True,
                        "alert_kind": "funding_failure",
                        "failure_reason": str(exc),
                        "method": method,
                        "is_resolved": False,
                    },
                )
            raise

        # Zūm accepted the AP create. Mark complete locally and activate the loan
        # immediately (Interac/EFT often stay InProgress at Zūm for a long time).
        # Failure / Returned / Cancelled webhooks reopen the loan for retry.
        funding.processor_transaction_id = processor_id
        funding.reference = processor_id
        funding.failure_reason = None
        funding.status = "completed"
        funding.completed_at = timezone.now()
        funding.save(update_fields=[
            "processor_transaction_id",
            "reference",
            "failure_reason",
            "status",
            "completed_at",
            "updated_at",
        ])

        loan.refresh_from_db()
        loan.mark_funding_completed(
            method=method,
            reference=processor_id,
            user=user,
        )

        from activity.services import actor_label, resolve_funding_failure_alerts

        resolve_funding_failure_alerts(loan, reason="funding_completed")

        recommended_method = FundingMethodRecommendation.for_date()
        actor = actor_label(user)
        log_activity(
            loan,
            "loan_funded",
            "Funding Completed",
            (
                f"Funding accepted by ZūmRails via {method.upper()} and marked complete by "
                f"{actor}. Loan activated; a failure webhook will reopen funding if Zūm "
                "later reports a failure."
            ),
            created_by=getattr(user, "id", "system"),
            metadata={
                "funded_payment_id": str(funding.id),
                "method": method,
                "recommended_method": recommended_method,
                "overrode_recommendation": bool(
                    recommended_method and recommended_method != method
                ),
                "loan_id": str(loan.id),
                "completed_on_create": True,
            },
        )
        FundingService._queue_funding_email(loan)
        return funding

    @staticmethod
    def sync_active_funding_from_zum(loan: Loan) -> FundedPayment | None:
        """Refresh processing funding from Zum by transaction id (webhook fallback)."""
        # Heal completed AP that never flipped the loan to active.
        if loan.status == "pending_funding":
            completed = (
                FundedPayment.objects.filter(loan=loan, status="completed")
                .order_by("-created_at")
                .first()
            )
            if completed:
                with transaction.atomic():
                    locked = FundedPayment.objects.select_for_update().select_related(
                        "loan"
                    ).get(pk=completed.pk)
                    return apply_funded_payment_zum_status(
                        locked,
                        status_value="Completed",
                        reason=None,
                    )

        funding = get_active_funding_attempt(loan)
        if not funding or funding.status != "processing":
            return funding

        # Heal rows that already stored a terminal Zum status but stayed processing
        # (e.g. Cancelled was saved only on zum_status before unlock logic existed).
        local_zum_status = normalize_zum_status(funding.zum_status)
        if local_zum_status and (
            is_terminal_zum_funding_status(local_zum_status)
            or local_zum_status == "Completed"
        ):
            with transaction.atomic():
                funding = FundedPayment.objects.select_for_update().select_related(
                    "loan"
                ).get(pk=funding.pk)
                if funding.status == "processing" or (
                    local_zum_status == "Completed" and funding.loan.status != "active"
                ):
                    return apply_funded_payment_zum_status(
                        funding,
                        status_value=local_zum_status,
                        reason=funding.failure_reason or local_zum_status,
                    )
            return funding

        tx_id = (funding.processor_transaction_id or "").strip()
        if not tx_id:
            return funding
        try:
            payload = ZumRailsService.get_transaction(tx_id)
        except ZumRailsError:
            logger.exception(
                "Unable to sync funding status from Zūm for loan=%s tx=%s",
                loan.id,
                tx_id,
            )
            return funding

        fields = extract_zum_transaction_fields(payload)
        status_value = fields.get("status")
        reason = fields.get("reason")
        if not status_value and not reason:
            return funding

        with transaction.atomic():
            funding = FundedPayment.objects.select_for_update().select_related("loan").get(
                pk=funding.pk
            )
            if funding.status != "processing" and normalize_zum_status(status_value) != "Completed":
                return funding
            return apply_funded_payment_zum_status(
                funding,
                status_value=status_value,
                reason=reason,
            )

    @staticmethod
    @transaction.atomic
    def release_stuck_funding(loan: Loan, *, user=None) -> FundedPayment:
        """Mark an unsubmitted processing attempt failed so staff can fund again.

        Only allowed when Zūm never returned a processor transaction id. Attempts
        that already have a Zūm id stay blocked until a webhook settles them.
        """
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        funding = (
            FundedPayment.objects.select_for_update()
            .filter(loan=loan, status="processing")
            .order_by("-created_at")
            .first()
        )
        if not funding:
            raise ValueError("No stuck funding attempt to release.")
        if not is_unsubmitted_funding_attempt(funding):
            raise ValueError(
                "Only funding attempts that never received a Zūm transaction id "
                "can be released. Wait for the Zūm webhook or contact support."
            )

        reason = (funding.failure_reason or "").strip() or (
            "Stuck funding attempt released for retry"
        )
        if "Released for retry" not in reason:
            reason = f"{reason} (Released for retry)"
        funding.status = "failed"
        funding.failure_reason = reason
        funding.save(update_fields=["status", "failure_reason", "updated_at"])
        release_funding_locks(loan)

        from activity.services import actor_label

        actor = actor_label(user)
        log_activity(
            loan,
            "system",
            "Stuck Funding Released",
            (
                f"Stuck funding attempt released by {actor} so funding can be retried. "
                f"Zūm reason: {funding.failure_reason}."
            ),
            created_by=getattr(user, "id", "system"),
            metadata={
                "funded_payment_id": str(funding.id),
                "failure_reason": funding.failure_reason,
                "loan_id": str(loan.id),
            },
        )
        return funding

    @staticmethod
    def _complete_card_issuance(loan, *, collections_account, user):
        destination_snapshot = {
            "method": "card_issuance",
            "arrive_application_id": getattr(loan.customer, "arrive_application_id", None),
            "zum_user_id": getattr(loan.customer, "arrive_zum_user_id", None),
        }
        funding = FundedPayment.objects.create(
            loan=loan,
            amount=loan.principal,
            method="card_issuance",
            status="completed",
            destination_snapshot=destination_snapshot,
            collections_account_snapshot=(
                account_snapshot(collections_account) if collections_account else {}
            ),
            initiated_by=user,
            reference=f"CARD-{loan.customer.arrive_application_id or loan.id}",
            processor_transaction_id=f"card-issuance-{uuid.uuid4().hex[:12]}",
            completed_at=timezone.now(),
            notes="Card issuance funding (Arrive webhook / secured card).",
        )
        loan.funding_method = "card_issuance"
        loan.funding_reference = funding.reference
        loan.funding_destination = destination_snapshot
        if collections_account:
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
            method="card_issuance",
            reference=funding.reference,
            user=user,
        )
        from activity.services import actor_label, resolve_funding_failure_alerts

        resolve_funding_failure_alerts(loan, reason="funding_completed")
        log_activity(
            loan,
            "system",
            "Card Issuance Funding",
            f"Loan marked funded via Card Issuance by {actor_label(user)} (no MohawkLoans EFT/EMT).",
            created_by=getattr(user, "id", "system"),
            metadata={"funded_payment_id": str(funding.id), "method": "card_issuance", "loan_id": str(loan.id)},
        )
        FundingService._queue_funding_email(loan)

        from accounts.arrive_integration import queue_funding_webhook

        queue_funding_webhook(loan, funding)
        return funding

    @staticmethod
    def _queue_funding_email(loan):
        template_name = "Fund/Approve Template"
        from communications.models import CommunicationTemplate
        from communications.tasks import send_template_message

        template = CommunicationTemplate.objects.filter(
            name=template_name,
            type="email",
            is_active=True,
        ).first()
        if template and not loan.communications.filter(
            direction="outbound",
            type="email",
            template_name=template_name,
        ).exists():
            customer_id = str(loan.customer_id)
            loan_id = str(loan.id)
            template_id = str(template.id)
            transaction.on_commit(
                lambda: send_template_message.delay(customer_id, template_id, loan_id)
            )


class CollectionService:
    @staticmethod
    def initiate(loan: Loan, *, amount, user, payment: Payment | None = None):
        try:
            with transaction.atomic():
                loan = Loan.objects.select_for_update().select_related("customer").get(pk=loan.pk)
                if loan.status == "stopped":
                    raise ValueError("Collections are stopped on this loan.")
                if payment:
                    payment = Payment.objects.select_for_update().get(
                        pk=payment.pk,
                        loan=loan,
                    )
                    if payment.status == "unscheduled":
                        raise ValueError("Unscheduled payments are not sent to Zum.")
                    if payment.collection_attempts.filter(
                        status__in=["processing", "completed"]
                    ).exists():
                        raise ValueError("Collection already exists for this payment.")

                account = loan.collections_account or loan.bank_account
                if not account:
                    raise ValueError("Collections account required")

                collection = CollectionPayment.objects.create(
                    loan=loan,
                    payment=payment,
                    amount=amount,
                    status="processing",
                    account_snapshot=account_snapshot(account),
                    settlement_due_at=add_business_days(timezone.now(), 4),
                    initiated_by=user,
                )
                if payment and payment.status == "scheduled":
                    payment.status = "pending"
                    payment.save(update_fields=["status"])
        except IntegrityError as exc:
            raise ValueError("Collection already exists for this payment.") from exc

        zum_user = ZumRailsService.user_payload(loan.customer, account=account)
        try:
            processor_id = ZumRailsService.initiate_transaction(
                amount=collection.amount,
                transaction_type="AccountsReceivable",
                method="Eft",
                memo=f"Loan {str(loan.id)[:8]}",
                comment=f"Loan repayment {str(loan.id)[:8]}",
                client_transaction_id=collection.id,
                user_payload=zum_user,
            )
        except ZumRailsError as exc:
            if isinstance(exc, ZumRailsRequestError) and not exc.outcome_unknown:
                collection.status = "failed"
            collection.failure_reason = str(exc)
            collection.save(update_fields=["status", "failure_reason", "updated_at"])
            raise

        collection.processor_transaction_id = processor_id
        collection.failure_reason = None
        collection.save(update_fields=[
            "processor_transaction_id",
            "failure_reason",
            "updated_at",
        ])

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
        loan = Loan.objects.select_for_update().get(pk=loan.pk)

        if failed_payment.loan_id != loan.pk:
            raise ValueError("Failed payment not found for this loan.")
        if failed_payment.status not in ("failed", "returned", "rejected"):
            raise ValueError(
                "Collections account cannot be changed until a collection payment has failed."
            )
        if new_account.customer_id != loan.customer_id:
            raise ValueError("Collections account must belong to this customer.")
        assert_payment_account_allowed(new_account, role="Collections account")

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
        from activity.services import actor_label, format_account_label, log_staff_action

        actor = actor_label(user)
        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value="system",
            title="Collections Account Changed",
            description=(
                f"Collections account changed from {format_account_label(previous)} "
                f"to {format_account_label(new)} by {actor} "
                f"(after failed EFT collection)."
            ),
            metadata={"audit_id": str(audit.id), "failed_payment_id": str(failed_payment.id)},
        )
        return audit


class FundingConfigurationService:
    # Staff may pick funding/collections accounts before approve and before fund.
    CONFIGURABLE_STATUSES = frozenset(
        {"ibv_pending", "pending", "pending_signature", "pending_funding"}
    )

    @staticmethod
    def _emt_label(destination: dict) -> str:
        emt = destination.get("emt") if isinstance(destination.get("emt"), dict) else {}
        email = emt.get("email") or "(none)"
        source = emt.get("source") or ""
        return f"{email}" + (f" ({source})" if source else "")

    @staticmethod
    def default_customer_payment_account(customer) -> BankAccount | None:
        """Primary / EFT-flagged payable account — same preference the staff UI shows."""
        qs = BankAccount.objects.filter(customer=customer, connection__is_active=True)
        return (
            qs.filter(use_for_eft_funding=True).order_by("-is_primary", "name").first()
            or qs.filter(is_primary=True).order_by("name").first()
            or qs.filter(type__in=["checking", "chequing", "savings"])
            .order_by("-is_primary", "name")
            .first()
            or qs.order_by("-is_primary", "name").first()
        )

    @staticmethod
    def ensure_defaults(loan: Loan, *, user=None) -> Loan:
        """Persist the customer's default bank when the loan has no destinations yet.

        The staff UI pre-fills the primary account locally. Without saving those
        choices onto the loan, readiness still reports "Funding destination /
        Collections account required" and Fund Customer stays disabled.

        This is page-load hydration, not a staff decision. Activity History
        attributes it to System even if a viewer user is passed in.
        """
        if loan.status not in FundingConfigurationService.CONFIGURABLE_STATUSES:
            return loan
        try:
            assert_funding_configuration_editable(loan)
        except ValueError:
            return loan

        arrive = is_arrive_funded_loan(loan)
        configured = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}
        emt = configured.get("emt") if isinstance(configured.get("emt"), dict) else {}
        eft = configured.get("eft") if isinstance(configured.get("eft"), dict) else {}
        emt_configured = bool(emt.get("email"))
        eft_configured = bool(
            eft.get("account") or eft.get("bank_account_id") or loan.bank_account_id
        )
        collections_configured = bool(loan.collections_account_id or loan.bank_account_id)

        needs_funding = not arrive and not emt_configured and not eft_configured
        needs_collections = not collections_configured
        if not needs_funding and not needs_collections:
            return loan

        account = FundingConfigurationService.default_customer_payment_account(loan.customer)
        if not account:
            return loan

        kwargs = {}
        if needs_funding:
            kwargs["eft_bank_account_id"] = account.id
            kwargs["collections_account_id"] = account.id
        elif needs_collections:
            kwargs["collections_account_id"] = account.id

        # Page-load hydration is not a staff choice — always attribute to System.
        return FundingConfigurationService.configure(loan, user=None, **kwargs)

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
        loan = Loan.objects.select_for_update().get(pk=loan.pk)

        if loan.status not in FundingConfigurationService.CONFIGURABLE_STATUSES:
            raise ValueError(
                "Account destinations can only be changed before funding is locked "
                "(pending review through pending funding)."
            )
        assert_funding_configuration_editable(loan)

        from activity.services import actor_label, format_account_label, log_staff_action

        destination = loan.funding_destination
        if not isinstance(destination, dict):
            destination = {}
        else:
            destination = dict(destination)

        previous_eft_snap = None
        if isinstance(destination.get("eft"), dict):
            previous_eft_snap = destination["eft"].get("account")
        previous_eft = format_account_label(previous_eft_snap or loan.bank_account)
        previous_collections = format_account_label(loan.collections_account)
        previous_emt = FundingConfigurationService._emt_label(destination)

        update_fields = ["funding_destination", "updated_at"]
        changes = []

        if emt_email:
            destination["emt"] = {
                "email": str(emt_email),
                "source": emt_source or "application",
            }
            new_emt = FundingConfigurationService._emt_label(destination)
            if new_emt != previous_emt:
                changes.append(f"EMT destination changed from {previous_emt} to {new_emt}")

        if eft_bank_account_id:
            try:
                account = BankAccount.objects.get(id=eft_bank_account_id, customer=loan.customer)
                assert_payment_account_allowed(account, role="Funding account")
                loan.bank_account = account
                destination["eft"] = {
                    "bank_account_id": str(account.id),
                    "account": account_snapshot(account),
                }
                update_fields.append("bank_account")
                new_eft = format_account_label(account)
                if new_eft != previous_eft:
                    changes.append(f"Funding account changed from {previous_eft} to {new_eft}")
            except BankAccount.DoesNotExist:
                raise ValueError("Selected EFT account must belong to this customer.")

        if collections_account_id:
            try:
                account = BankAccount.objects.get(id=collections_account_id, customer=loan.customer)
                assert_payment_account_allowed(account, role="Collections account")
                loan.collections_account = account
                update_fields.append("collections_account")
                new_collections = format_account_label(account)
                if new_collections != previous_collections:
                    changes.append(
                        f"Collections account changed from {previous_collections} to {new_collections}"
                    )
            except BankAccount.DoesNotExist:
                raise ValueError("Selected collections account must belong to this customer.")

        loan.funding_destination = destination
        loan.save(update_fields=update_fields)

        actor = actor_label(user)
        automatic = user is None
        actor_phrase = (
            "by System (applied automatically from the customer's verified bank)"
            if automatic
            else f"by {actor}"
        )
        if changes:
            description = f"{'; '.join(changes)} {actor_phrase}."
        else:
            description = (
                f"Funding destination and collections account selections were saved {actor_phrase}."
            )

        log_staff_action(
            customer=loan.customer,
            loan=loan,
            user=user,
            type_value="system",
            title="Funding Configured",
            description=description,
            metadata={
                "action": "funding_configured",
                "changes": changes,
                "source": "auto_default" if automatic else "staff",
                "actor": "System" if automatic else actor,
            },
        )
        return loan


class SettlementService:
    @staticmethod
    @transaction.atomic
    def sync_from_zum(collection: CollectionPayment) -> CollectionPayment:
        """Apply a Zūm collection failure that webhooks missed. Never auto-completes."""
        # Postgres cannot FOR UPDATE a nullable payment outer join.
        collection = CollectionPayment.objects.select_for_update(of=("self",)).select_related(
            "loan",
            "payment",
        ).get(pk=collection.pk)
        if collection.status in ("failed", "returned", "rejected", "cancelled"):
            return collection

        local_status = collection.zum_status
        local_reason = collection.failure_reason if collection.status == "processing" else None
        if is_terminal_zum_collection_failure(local_status, local_reason):
            apply_collection_failure(
                collection,
                reason=(local_reason or local_status or "Failed"),
                status=collection_failure_status_from_zum(local_status),
            )
            if local_status:
                collection.zum_status = local_status
                collection.save(update_fields=["zum_status", "updated_at"])
            return collection

        tx_id = (collection.processor_transaction_id or "").strip()
        if not tx_id:
            return collection
        try:
            payload = ZumRailsService.get_transaction(tx_id)
        except ZumRailsError:
            logger.exception(
                "Unable to sync collection status from Zūm collection=%s tx=%s",
                collection.id,
                tx_id,
            )
            return collection

        fields = extract_zum_transaction_fields(payload)
        status_value = fields.get("status")
        reason = fields.get("reason") or status_value
        if not is_terminal_zum_collection_failure(status_value, reason):
            if status_value and collection.status == "processing":
                collection.zum_status = status_value
                collection.save(update_fields=["zum_status", "updated_at"])
            return collection

        apply_collection_failure(
            collection,
            reason=reason or status_value or "Failed",
            status=collection_failure_status_from_zum(status_value),
        )
        if status_value:
            collection.zum_status = status_value
            collection.save(update_fields=["zum_status", "updated_at"])
        return collection

    @staticmethod
    def reconcile_missed_failures(*, limit: int = 25, local_only: bool = False) -> int:
        """Heal collections Zūm already failed but local status never flipped.

        ``local_only`` skips Zūm HTTP lookups so staff list endpoints cannot
        500/timeout when the processor is slow or unreachable.
        """
        now = timezone.now()
        seen = set()
        healed = 0
        candidate_lists = [
            CollectionPayment.objects.filter(status="processing").filter(
                Q(zum_status__icontains="Failed")
                | Q(zum_status__iexact="Returned")
                | Q(zum_status__iexact="Rejected")
            ),
        ]
        if not local_only:
            candidate_lists.extend([
                CollectionPayment.objects.filter(
                    status="completed",
                    processor_transaction_id__isnull=False,
                    initiated_at__gte=now - timedelta(days=60),
                    initiated_at__lte=now - timedelta(days=4),
                ).filter(
                    Q(zum_status__isnull=True)
                    | ~Q(zum_status__iexact="Completed")
                ).order_by("initiated_at"),
                CollectionPayment.objects.filter(
                    status="processing",
                    processor_transaction_id__isnull=False,
                    settlement_due_at__lte=now,
                ).order_by("initiated_at"),
            ])
        remaining = limit
        for qs in candidate_lists:
            for collection in qs:
                if collection.pk in seen:
                    continue
                if remaining <= 0:
                    return healed
                seen.add(collection.pk)
                remaining -= 1
                previous_status = collection.status
                try:
                    synced = SettlementService.sync_from_zum(collection)
                except Exception:
                    logger.exception(
                        "Unable to reconcile collection failure collection=%s",
                        collection.id,
                    )
                    continue
                if (
                    synced.status in ("failed", "returned", "rejected")
                    and synced.status != previous_status
                ):
                    healed += 1
        return healed

    @staticmethod
    @transaction.atomic
    def complete_if_eligible(collection: CollectionPayment):
        collection = CollectionPayment.objects.select_for_update().select_related("loan").get(pk=collection.pk)
        if collection.status != "processing":
            return False
        if not collection.processor_transaction_id or not collection.settlement_due_at:
            return False
        if collection.settlement_due_at > timezone.now():
            return False
        if any((event.get("event") in ALL_FAILURE_EVENTS) for event in (collection.event_history or [])):
            return False

        collection = SettlementService.sync_from_zum(collection)
        if collection.status != "processing":
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
        from .services import LoanService

        # Manual/Interac credits may have shortened the remaining schedule against
        # the last settled balance while this PAD was still processing.
        LoanService._trim_scheduled_payments_to_balance(collection.loan)
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
        missing_due = CollectionPayment.objects.filter(
            status="processing",
            processor_transaction_id__isnull=False,
            settlement_due_at__isnull=True,
        )
        for collection in missing_due:
            collection.settlement_due_at = add_business_days(collection.initiated_at, 4)
            collection.save(update_fields=["settlement_due_at", "updated_at"])

        SettlementService.reconcile_missed_failures()

        due = CollectionPayment.objects.filter(
            status="processing",
            processor_transaction_id__isnull=False,
            settlement_due_at__lte=timezone.now(),
        )
        completed = 0
        for collection in due:
            if SettlementService.complete_if_eligible(collection):
                completed += 1
        return completed
