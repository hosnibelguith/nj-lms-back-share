"""Twilio SMS provider.

Outbound messages go through the Twilio Messages API; delivery receipts and
inbound replies arrive on the webhook handled in views.TwilioWebhookView.
"""

import logging
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Twilio MessageStatus -> Communication.status.
# Unlisted values intentionally leave the record untouched rather than guessing.
STATUS_MAP = {
    'accepted': 'pending',
    'scheduled': 'pending',
    'queued': 'pending',
    'sending': 'pending',
    'sent': 'sent',
    'delivered': 'delivered',
    'read': 'read',
    'undelivered': 'failed',
    'failed': 'failed',
    'canceled': 'failed',
}

# Twilio error codes meaning the destination must not be texted again.
OPT_OUT_ERROR_CODES = {
    '21610',  # Recipient has unsubscribed (replied STOP)
    '21211',  # Invalid 'To' number
    '21612',  # Cannot route to this number
    '21614',  # 'To' number is not a valid mobile number
}

# Carrier-reserved keywords that must stop and resume messaging.
STOP_KEYWORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT', 'ARRET'}
START_KEYWORDS = {'START', 'YES', 'UNSTOP'}


class TwilioError(ValueError):
    """Base error for failures communicating with Twilio."""


class TwilioConfigurationError(TwilioError):
    """Raised when required Twilio settings are missing or the input is unusable."""


class TwilioRequestError(TwilioError):
    """Raised when Twilio rejects or does not answer a request."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = str(code) if code is not None else None


def normalize_e164(phone) -> str:
    """Twilio requires E.164 (+14165551234). Returns '' when unusable."""
    raw = str(phone or '').strip()
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    if raw.startswith('+') and len(digits) >= 11:
        return f'+{digits}'
    return ''


def phone_last10(phone) -> str:
    digits = re.sub(r'\D', '', str(phone or ''))
    return digits[-10:] if len(digits) >= 10 else digits


def map_message_status(message_status) -> str | None:
    return STATUS_MAP.get(str(message_status or '').strip().lower())


def is_opt_out_error(error_code) -> bool:
    return str(error_code or '').strip() in OPT_OUT_ERROR_CODES


def classify_inbound_keyword(message) -> str | None:
    """Return 'stop', 'start', or None for a customer's inbound text."""
    word = re.sub(r'[^A-Za-z]', '', str(message or '')).upper()
    if word in STOP_KEYWORDS:
        return 'stop'
    if word in START_KEYWORDS:
        return 'start'
    return None


def set_sms_opt_out(customer, *, opted_out: bool, reason: str = '') -> bool:
    """Record an SMS consent change. Returns True when the state actually moved."""
    from activity.models import ActivityHistory

    if customer is None or customer.sms_opted_out == opted_out:
        return False

    customer.sms_opted_out = opted_out
    customer.sms_opted_out_at = timezone.now() if opted_out else None
    customer.sms_opt_out_reason = reason or None
    customer.save(
        update_fields=['sms_opted_out', 'sms_opted_out_at', 'sms_opt_out_reason']
    )
    ActivityHistory.objects.create(
        customer=customer,
        type='sms_received' if opted_out else 'sms_sent',
        title='SMS Opt-Out' if opted_out else 'SMS Opt-In',
        description=(
            f'Customer opted out of SMS ({reason}).'
            if opted_out
            else 'Customer opted back in to SMS.'
        ),
    )
    return True


class TwilioService:
    @classmethod
    def _setting(cls, key: str, default=''):
        from accounts.models import GlobalSetting

        value = GlobalSetting.get_value(key, getattr(settings, key, default))
        return value if value is not None else default

    @classmethod
    def _credentials(cls):
        account_sid = str(cls._setting('TWILIO_ACCOUNT_SID', '')).strip()
        auth_token = str(cls._setting('TWILIO_AUTH_TOKEN', '')).strip()
        if not account_sid or not auth_token:
            raise TwilioConfigurationError(
                'Twilio account SID and auth token are not configured.'
            )
        return account_sid, auth_token

    @classmethod
    def auth_token(cls) -> str:
        """Token the webhook uses to validate Twilio's request signature."""
        return cls._credentials()[1]

    @classmethod
    def _sender(cls) -> dict:
        """Prefer a Messaging Service; fall back to the single sending number."""
        messaging_service_sid = str(
            cls._setting('TWILIO_MESSAGING_SERVICE_SID', '')
        ).strip()
        if messaging_service_sid:
            return {'messaging_service_sid': messaging_service_sid}

        from_number = normalize_e164(cls._setting('TWILIO_PHONE_NUMBER', ''))
        if not from_number:
            raise TwilioConfigurationError(
                'Twilio phone number or messaging service SID is not configured.'
            )
        return {'from_': from_number}

    @classmethod
    def is_configured(cls) -> bool:
        try:
            cls._credentials()
            cls._sender()
        except TwilioConfigurationError:
            return False
        return True

    @classmethod
    def _client(cls):
        from twilio.rest import Client

        account_sid, auth_token = cls._credentials()
        return Client(account_sid, auth_token)

    @classmethod
    def send_sms(cls, *, to: str, content: str):
        """Send one SMS. Returns the Twilio message SID."""
        from twilio.base.exceptions import TwilioException, TwilioRestException

        destination = normalize_e164(to)
        if not destination:
            raise TwilioConfigurationError(f'Invalid destination phone number: {to!r}')
        if not str(content or '').strip():
            raise TwilioConfigurationError('SMS content is required.')

        payload = {'to': destination, 'body': content, **cls._sender()}
        status_callback = str(cls._setting('TWILIO_STATUS_CALLBACK_URL', '')).strip()
        if status_callback:
            payload['status_callback'] = status_callback

        try:
            message = cls._client().messages.create(**payload)
        except TwilioRestException as exc:
            raise TwilioRequestError(
                f'Twilio rejected the message ({exc.code}): {exc.msg}',
                code=exc.code,
            ) from exc
        except TwilioException as exc:
            raise TwilioRequestError(f'Twilio request failed: {exc}') from exc

        return message.sid
