import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import AuthOTPChallenge


OTP_TTL_MINUTES = 10
OTP_COOLDOWN_SECONDS = 60


def hash_otp(code: str) -> str:
    return hashlib.sha256(f"{code}:{settings.SECRET_KEY}".encode()).hexdigest()


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


@transaction.atomic
def create_otp_challenge(identifier: str, purpose: str, metadata: dict | None = None):
    now = timezone.now()

    recent = AuthOTPChallenge.objects.select_for_update().filter(
        identifier=identifier,
        purpose=purpose,
        status=AuthOTPChallenge.STATUS_PENDING,
        created_at__gte=now - timedelta(seconds=OTP_COOLDOWN_SECONDS),
    ).first()

    if recent:
        return recent, None, False

    AuthOTPChallenge.objects.filter(
        identifier=identifier,
        purpose=purpose,
        status=AuthOTPChallenge.STATUS_PENDING,
    ).update(status=AuthOTPChallenge.STATUS_EXPIRED)

    code = generate_otp()

    challenge = AuthOTPChallenge.objects.create(
        identifier=identifier,
        purpose=purpose,
        code_hash=hash_otp(code),
        expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
        metadata=metadata or {},
    )

    return challenge, code, True


@transaction.atomic
def verify_otp_challenge(challenge_id, code: str, purpose: str):
    try:
        challenge = AuthOTPChallenge.objects.select_for_update().get(
            id=challenge_id,
            purpose=purpose,
        )
    except AuthOTPChallenge.DoesNotExist:
        return None, "Invalid OTP challenge."

    if challenge.status != AuthOTPChallenge.STATUS_PENDING:
        return None, "OTP is no longer valid."

    if challenge.is_expired:
        challenge.status = AuthOTPChallenge.STATUS_EXPIRED
        challenge.save(update_fields=["status"])
        return None, "OTP expired."

    if challenge.attempts >= challenge.max_attempts:
        challenge.status = AuthOTPChallenge.STATUS_LOCKED
        challenge.save(update_fields=["status"])
        return None, "Too many attempts."

    if challenge.code_hash != hash_otp(code):
        challenge.attempts += 1
        update_fields = ["attempts"]

        if challenge.attempts >= challenge.max_attempts:
            challenge.status = AuthOTPChallenge.STATUS_LOCKED
            update_fields.append("status")

        challenge.save(update_fields=update_fields)
        return None, "Invalid OTP."

    challenge.status = AuthOTPChallenge.STATUS_VERIFIED
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["status", "consumed_at"])

    return challenge, None
