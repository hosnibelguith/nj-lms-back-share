import base64
import hashlib
import hmac
import logging
import re
import threading
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
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
ZUMRAILS_TOKEN_TTL_SECONDS = 55 * 60
ZUMRAILS_REQUEST_TIMEOUT_SECONDS = 15
ZUMRAILS_MEMO_PATTERN = re.compile(r"^[A-Za-z0-9 _-]{1,15}$")


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


def is_arrive_funded_loan(loan: Loan) -> bool:
    """Arrive funds the card after our decision webhook — never LendStack EFT/EMT."""
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
    arrive_loan = is_arrive_funded_loan(loan)
    if loan.status != "pending_funding":
        blockers.append("Loan is not pending funding.")
    if not loan.contract_signed:
        blockers.append("Contract must be signed before funding.")
    if loan.funded_payments.filter(status__in=["processing", "completed"]).exists():
        blockers.append("Funding already exists for this loan.")
    if not arrive_loan:
        if not emt_configured or not eft_configured:
            blockers.append("Funding destination required.")
        if not collections_configured:
            blockers.append("Collections account required.")
    elif not collections_configured:
        # Arrive funds via Card Issuance, but staff must still pick the
        # collections (repayment) bank account when multiple chequing accounts exist.
        blockers.append("Collections account required.")
    return {
        "emt_configured": emt_configured,
        "eft_configured": eft_configured,
        "collections_account_configured": collections_configured,
        "has_active_funding": loan.funded_payments.filter(status__in=["processing", "completed"]).exists(),
        "arrive_external_funding": arrive_loan,
        "recommended_method_override": "card_issuance" if arrive_loan else None,
        "allowed_methods": (
            ["card_issuance"] if arrive_loan else ["eft", "etransfer"]
        ),
        "blockers": blockers,
    }


def apply_collection_failure(collection: CollectionPayment, *, reason: str, status: str = "failed"):
    was_settled = collection.status == "completed"
    collection.status = status
    collection.failure_reason = reason
    collection.save(update_fields=["status", "failure_reason", "updated_at"])

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
            except (requests.RequestException, ValueError) as exc:
                raise ZumRailsRequestError(
                    "Unable to authenticate with ZūmRails.",
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
                return response.json()
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

    @staticmethod
    def _memo(value: str) -> str:
        memo = re.sub(r"[^A-Za-z0-9 _-]", "", str(value or ""))[:15].strip()
        if not ZUMRAILS_MEMO_PATTERN.fullmatch(memo):
            raise ZumRailsConfigurationError("ZūmRails memo is invalid.")
        return memo

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
            missing = [
                label for label, value in (
                    ("institution number", account.institution_number),
                    ("transit number", account.transit_number),
                    ("account number", account.account_number),
                )
                if not value
            ]
            if missing:
                raise ZumRailsConfigurationError(
                    f"Selected EFT account is missing {', '.join(missing)}."
                )
            payload["BankAccountInformation"] = {
                "InstitutionNumber": account.institution_number,
                "TransitNumber": account.transit_number,
                "AccountNumber": account.account_number,
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
            "Amount": float(amount),
            "Memo": cls._memo(memo),
            "ClientTransactionId": str(client_transaction_id),
            "User": user_payload,
            "WalletId": cls.get_wallet_id(),
        }
        if comment:
            payload["Comment"] = comment
        if method == "Interac":
            payload.update({
                "InteracNotificationChannel": "email",
                "InteracHasSecurityQuestionAndAnswer": False,
            })

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
                if loan.funded_payments.filter(status__in=["processing", "completed"]).exists():
                    raise ValueError("Funding already exists for this loan.")

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

                loan.funding_method = method
                loan.funding_reference = destination_snapshot.get("email") or str(funding.id)
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
        except IntegrityError as exc:
            raise ValueError("Funding already exists for this loan.") from exc

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

        try:
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
            if isinstance(exc, ZumRailsRequestError) and not exc.outcome_unknown:
                funding.status = "failed"
            funding.failure_reason = str(exc)
            funding.save(update_fields=["status", "failure_reason", "updated_at"])
            raise

        funding.processor_transaction_id = processor_id
        funding.reference = processor_id
        funding.failure_reason = None
        funding.save(update_fields=[
            "processor_transaction_id",
            "reference",
            "failure_reason",
            "updated_at",
        ])

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
                "overrode_recommendation": bool(
                    recommended_method and recommended_method != method
                ),
            },
        )
        FundingService._queue_funding_email(loan)
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
        log_activity(
            loan,
            "system",
            "Card Issuance Funding",
            "Loan marked funded via Card Issuance (no LendStack EFT/EMT).",
            created_by=getattr(user, "id", "system"),
            metadata={"funded_payment_id": str(funding.id), "method": "card_issuance"},
        )
        FundingService._queue_funding_email(loan)
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
                if payment:
                    payment = Payment.objects.select_for_update().get(
                        pk=payment.pk,
                        loan=loan,
                    )
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
    # Staff may pick funding/collections accounts before approve and before fund.
    CONFIGURABLE_STATUSES = frozenset(
        {"ibv_pending", "pending", "pending_signature", "pending_funding"}
    )

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
