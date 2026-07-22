"""Arrive ↔ LendStack integration: lead create, handoff tokens, resume, decision webhooks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import ArriveHandoffToken, Customer, GlobalSetting, User
from accounts.utils.phone import normalize_ca_phone
from loans.models import Loan
from loans.services import LoanService

logger = logging.getLogger(__name__)


class ArriveIdentityConflict(Exception):
    """Email/phone already linked to a different Arrive application or Zum user."""

    def __init__(self, message: str = "Identity conflict.", code: str = "identity_conflict"):
        self.message = message
        self.code = code
        super().__init__(message)


def _get_setting(key: str, default: str = "") -> str:
    value = GlobalSetting.get_value(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return str(getattr(settings, key, default) or default).strip()


def get_arrive_api_key() -> str:
    return _get_setting("ARRIVE_API_KEY", getattr(settings, "ARRIVE_API_KEY", ""))


def get_arrive_webhook_url() -> str:
    return _get_setting(
        "ARRIVE_WEBHOOK_URL",
        getattr(settings, "ARRIVE_WEBHOOK_URL", "https://app.arrivecard.ca/api/webhooks/lendstack/decision/"),
    )


def get_arrive_webhook_secret() -> str:
    return _get_setting("ARRIVE_WEBHOOK_SECRET", getattr(settings, "ARRIVE_WEBHOOK_SECRET", ""))


def get_portal_base_url() -> str:
    return _get_setting(
        "ARRIVE_PORTAL_BASE_URL",
        getattr(settings, "ARRIVE_PORTAL_BASE_URL", getattr(settings, "FRONTEND_URL", "http://localhost:3000")),
    ).rstrip("/")


def arrive_api_key_valid(provided: str | None) -> bool:
    expected = get_arrive_api_key()
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _handoff_ttl() -> timedelta:
    seconds = int(getattr(settings, "ARRIVE_HANDOFF_TOKEN_TTL_SECONDS", 1800) or 1800)
    return timedelta(seconds=max(60, seconds))


@transaction.atomic
def mint_handoff_token(customer: Customer) -> ArriveHandoffToken:
    return ArriveHandoffToken.objects.create(
        customer=customer,
        expires_at=timezone.now() + _handoff_ttl(),
    )


def build_application_url(token: ArriveHandoffToken) -> str:
    return f"{get_portal_base_url()}/customer/arrive/handoff?token={token.token}"


def get_primary_loan(customer: Customer) -> Loan | None:
    return customer.loans.order_by("-created_at").first()


def loan_decision_status(loan: Loan | None) -> str | None:
    if not loan:
        return None
    if loan.status in ("human_declined", "ai_declined") or loan.declined_at:
        return "declined"
    if loan.status in (
        "human_approved",
        "ai_approved",
        "pending_funding",
        "active",
        "paid_off",
    ) or loan.approved_at:
        return "approved"
    return None


def portal_status_for_loan(loan: Loan | None) -> str:
    decision = loan_decision_status(loan)
    if decision == "approved":
        return "approved"
    if decision == "declined":
        return "declined"
    return "application_in_progress"


@transaction.atomic
def consume_handoff_token(raw_token: str) -> tuple[Customer, dict[str, str]]:
    try:
        token_uuid = uuid.UUID(str(raw_token).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Invalid handoff token.") from exc

    handoff = (
        ArriveHandoffToken.objects.select_related("customer", "customer__portal_user")
        .select_for_update()
        .filter(token=token_uuid)
        .first()
    )
    if not handoff:
        raise ValueError("Invalid or unknown handoff token.")
    if handoff.is_consumed:
        raise ValueError("Handoff token has already been used.")
    if handoff.is_expired:
        raise ValueError("Handoff token has expired.")

    customer = handoff.customer
    portal_user = customer.portal_user
    if not portal_user or not portal_user.is_active:
        raise ValueError("Customer portal user is not available.")

    handoff.mark_consumed()

    refresh = RefreshToken.for_user(portal_user)
    return customer, {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


def _assert_identity_compatible(
    customer: Customer,
    *,
    arrive_application_id: str,
    zum_user_id: str,
) -> None:
    if customer.arrive_application_id and customer.arrive_application_id != arrive_application_id:
        raise ArriveIdentityConflict(
            "Email or phone is already linked to a different Arrive application.",
        )
    if customer.arrive_zum_user_id and customer.arrive_zum_user_id != zum_user_id:
        raise ArriveIdentityConflict(
            "Email or phone is already linked to a different Zum user.",
        )


@transaction.atomic
def create_or_resume_lead(payload: dict[str, Any]) -> tuple[Customer, Loan, ArriveHandoffToken, bool]:
    """
    Create or idempotently resume an Arrive-sourced lead.
    Returns (customer, loan, handoff_token, created).
    """
    event_id = str(payload["event_id"]).strip()
    arrive_application_id = str(payload["arrive_application_id"]).strip()
    zum_user_id = str(payload["zum_user_id"]).strip()
    email = str(payload["email"]).strip().lower()
    phone = str(payload["phone"]).strip()
    phone_normalized = normalize_ca_phone(phone)
    first_name = str(payload["first_name"]).strip()
    last_name = str(payload["last_name"]).strip()
    requested_amount = _money(payload["requested_loan_amount"])
    province = (payload.get("province") or "").strip().upper() or None
    date_of_birth = payload.get("date_of_birth")
    zum_user_card_id = (payload.get("zum_user_card_id") or "").strip() or None

    existing_by_event = (
        Customer.objects.select_for_update()
        .filter(arrive_event_id=event_id)
        .first()
    )
    if existing_by_event:
        loan = LoanService.create_initial_application(existing_by_event)
        token = mint_handoff_token(existing_by_event)
        return existing_by_event, loan, token, False

    existing_by_app = (
        Customer.objects.select_for_update()
        .filter(arrive_application_id=arrive_application_id)
        .first()
    )
    if existing_by_app:
        if existing_by_app.arrive_zum_user_id and existing_by_app.arrive_zum_user_id != zum_user_id:
            raise ArriveIdentityConflict("Arrive application is linked to a different Zum user.")
        existing_by_app.arrive_zum_user_id = zum_user_id
        if zum_user_card_id:
            existing_by_app.arrive_zum_user_card_id = zum_user_card_id
        existing_by_app.requested_loan_amount = requested_amount
        if not existing_by_app.arrive_event_id:
            existing_by_app.arrive_event_id = event_id
        existing_by_app.save()
        loan = LoanService.create_initial_application(existing_by_app)
        token = mint_handoff_token(existing_by_app)
        return existing_by_app, loan, token, False

    email_match = Customer.objects.select_for_update().filter(email__iexact=email).first()
    phone_match = None
    if phone_normalized:
        phone_match = Customer.objects.select_for_update().filter(phone_normalized=phone_normalized).first()

    for match in (email_match, phone_match):
        if match:
            _assert_identity_compatible(
                match,
                arrive_application_id=arrive_application_id,
                zum_user_id=zum_user_id,
            )

    customer = email_match or phone_match
    created = False

    if customer:
        customer.source = Customer.SOURCE_ARRIVE
        customer.arrive_application_id = arrive_application_id
        customer.arrive_zum_user_id = zum_user_id
        customer.arrive_zum_user_card_id = zum_user_card_id
        customer.arrive_event_id = event_id
        customer.requested_loan_amount = requested_amount
        customer.first_name = first_name or customer.first_name
        customer.last_name = last_name or customer.last_name
        customer.phone = phone
        customer.phone_normalized = phone_normalized
        if province:
            customer.province = province
        if date_of_birth:
            customer.date_of_birth = date_of_birth
        if not customer.portal_user:
            portal_user = User(
                email=email,
                full_name=f"{first_name} {last_name}".strip() or email,
                phone=phone,
                phone_normalized=phone_normalized,
                user_type="customer",
                is_staff=False,
            )
            portal_user.set_unusable_password()
            portal_user.save()
            customer.portal_user = portal_user
        if customer.onboarding_stage == "password_setup":
            customer.onboarding_stage = "banking_verification"
        customer.save()
    else:
        if User.objects.filter(email__iexact=email).exists():
            raise ArriveIdentityConflict("A user with this email already exists.")

        portal_user = User(
            email=email,
            full_name=f"{first_name} {last_name}".strip() or email,
            phone=phone,
            phone_normalized=phone_normalized,
            user_type="customer",
            is_staff=False,
        )
        portal_user.set_unusable_password()
        portal_user.save()

        customer = Customer.objects.create(
            portal_user=portal_user,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            phone_normalized=phone_normalized,
            date_of_birth=date_of_birth,
            province=province,
            requested_loan_amount=requested_amount,
            onboarding_stage="banking_verification",
            phone_verified=True,
            phone_verified_at=timezone.now(),
            source=Customer.SOURCE_ARRIVE,
            arrive_application_id=arrive_application_id,
            arrive_zum_user_id=zum_user_id,
            arrive_zum_user_card_id=zum_user_card_id,
            arrive_event_id=event_id,
        )
        created = True

    loan = LoanService.create_initial_application(customer)
    token = mint_handoff_token(customer)
    return customer, loan, token, created


def lead_response_payload(customer: Customer, loan: Loan, token: ArriveHandoffToken) -> dict[str, Any]:
    return {
        "lendstack_customer_id": str(customer.id),
        "loan_id": str(loan.id),
        "arrive_application_id": customer.arrive_application_id,
        "zum_user_id": customer.arrive_zum_user_id,
        "application_url": build_application_url(token),
        "expires_at": token.expires_at.isoformat().replace("+00:00", "Z"),
        "status": portal_status_for_loan(loan),
    }


def resume_portal_session(
    *,
    arrive_application_id: str,
    zum_user_id: str,
    loan_id: str,
) -> dict[str, Any]:
    try:
        loan_uuid = uuid.UUID(str(loan_id).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Invalid loan_id.") from exc

    loan = (
        Loan.objects.select_related("customer")
        .filter(id=loan_uuid)
        .first()
    )
    if not loan:
        raise LookupError("Unknown application or loan.")

    customer = loan.customer
    if customer.arrive_application_id != arrive_application_id.strip():
        raise LookupError("Unknown application or loan.")
    if customer.arrive_zum_user_id != zum_user_id.strip():
        raise LookupError("Unknown application or loan.")

    token = mint_handoff_token(customer)
    decision = loan_decision_status(loan)
    return {
        "portal_embed_url": build_application_url(token),
        "expires_at": token.expires_at.isoformat().replace("+00:00", "Z"),
        "status": portal_status_for_loan(loan),
        "decision": decision,
        "lendstack_customer_id": str(customer.id),
        "loan_id": str(loan.id),
    }


def _decline_reasons(loan: Loan) -> list[str]:
    reason = (loan.decline_reason or "").strip()
    if not reason:
        return ["Application declined."]
    parts = [p.strip() for p in reason.replace("\r", "").split("\n") if p.strip()]
    return parts or [reason]


def build_decision_payload(loan: Loan, *, decision: str) -> dict[str, Any]:
    customer = loan.customer
    requested = _money(customer.requested_loan_amount or loan.principal)
    approved_amount = None
    decline_reasons: list[str] = []

    if decision == "approved":
        approved_amount = f"{_money(loan.principal):.2f}"
    else:
        decline_reasons = _decline_reasons(loan)

    decided_at = loan.approved_at or loan.declined_at or timezone.now()
    if timezone.is_naive(decided_at):
        decided_at = timezone.make_aware(decided_at, timezone.get_current_timezone())

    return {
        "event_id": str(uuid.uuid4()),
        "event": "loan.decision",
        "decision": decision,
        "arrive_application_id": customer.arrive_application_id,
        "zum_user_id": customer.arrive_zum_user_id,
        "lendstack_customer_id": str(customer.id),
        "loan_id": str(loan.id),
        "requested_amount": f"{requested:.2f}",
        "approved_amount": approved_amount,
        "currency": "CAD",
        "decline_reasons": decline_reasons,
        "decided_at": decided_at.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def sign_arrive_webhook(raw_body: bytes, timestamp: str, secret: str) -> str:
    message = f"{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def deliver_decision_webhook(loan_id: str, decision: str) -> bool:
    try:
        loan = Loan.objects.select_related("customer").get(id=loan_id)
    except Loan.DoesNotExist:
        logger.error("Arrive decision webhook: loan %s not found", loan_id)
        return False

    customer = loan.customer
    if customer.source != Customer.SOURCE_ARRIVE or not customer.arrive_application_id:
        logger.info("Skipping Arrive webhook for non-Arrive loan %s", loan_id)
        return False

    url = get_arrive_webhook_url()
    secret = get_arrive_webhook_secret()
    if not url or not secret:
        logger.error("Arrive webhook URL/secret not configured; cannot notify for loan %s", loan_id)
        return False

    payload = build_decision_payload(loan, decision=decision)
    raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(timezone.now().timestamp()))
    signature = sign_arrive_webhook(raw_body, timestamp, secret)

    headers = {
        "Content-Type": "application/json",
        "X-LendStack-Signature": signature,
        "X-LendStack-Timestamp": timestamp,
        "User-Agent": "LendStack-ArriveWebhook/1.0",
    }

    try:
        response = requests.post(url, data=raw_body, headers=headers, timeout=20)
        if 200 <= response.status_code < 300:
            logger.info(
                "Arrive decision webhook delivered loan=%s decision=%s event_id=%s",
                loan_id,
                decision,
                payload["event_id"],
            )
            return True
        logger.warning(
            "Arrive decision webhook failed loan=%s status=%s body=%s",
            loan_id,
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Arrive decision webhook error for loan %s", loan_id)
        raise

    return False


def queue_decision_webhook(loan: Loan, decision: str) -> None:
    if not loan.customer_id:
        return
    customer = loan.customer
    if customer.source != Customer.SOURCE_ARRIVE:
        return
    from django.db import transaction
    from accounts.tasks import send_arrive_decision_webhook_task

    loan_id = str(loan.id)
    transaction.on_commit(lambda: send_arrive_decision_webhook_task.delay(loan_id, decision))
