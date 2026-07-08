import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from html import unescape

import requests
from django.conf import settings
from django.db import IntegrityError

from accounts.models import Customer
from communications.models import Communication

logger = logging.getLogger(__name__)


def html_to_text(html):
    if not html:
        return ""

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"(?is)<head.*?>.*?</head>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class InboundEmailPollResult:
    scanned: int = 0
    created: int = 0
    duplicates: int = 0
    skipped_unknown_customer: int = 0
    created_unknown_sender: int = 0
    skipped_missing_sender: int = 0
    errors: int = 0

    def as_dict(self):
        return {
            "scanned": self.scanned,
            "created": self.created,
            "duplicates": self.duplicates,
            "skipped_unknown_customer": self.skipped_unknown_customer,
            "created_unknown_sender": self.created_unknown_sender,
            "skipped_missing_sender": self.skipped_missing_sender,
            "errors": self.errors,
        }


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def _get_message_body(message):
    text_body = ""
    html_body = ""

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain" and not text_body:
                text_body = decoded.strip()
            elif content_type == "text/html" and not html_body:
                html_body = decoded.strip()
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")

            if message.get_content_type() == "text/html":
                html_body = decoded.strip()
            else:
                text_body = decoded.strip()

    return text_body or html_to_text(html_body) or "(No message body)", html_body or None


def _message_external_id(message, uid):
    message_id = _decode(message.get("Message-ID"))
    if message_id:
        return f"imap:{message_id}"[:255]
    return f"imap:uid:{uid}"[:255]


def _create_inbound_communication(
    *,
    sender_email,
    recipient_email,
    subject,
    content,
    html_content=None,
    external_id,
):
    sender_email = (sender_email or "").lower().strip()
    if not sender_email:
        return "missing_sender"

    customer = Customer.objects.filter(email__iexact=sender_email).first()
    is_unknown_sender = customer is None

    if Communication.objects.filter(
        type="email",
        direction="inbound",
        external_id=external_id,
    ).exists():
        return "duplicate"

    try:
        Communication.objects.create(
            customer=customer,
            type="email",
            direction="inbound",
            subject=subject or "(No subject)",
            from_address=sender_email,
            to_address=recipient_email,
            content=content or "(No message body)",
            html_content=html_content,
            status="delivered",
            external_id=external_id,
            incoming_status="new",
            is_answered=False,
            is_unknown_sender=is_unknown_sender,
        )
    except IntegrityError:
        return "duplicate"

    return "created_unknown_sender" if is_unknown_sender else "created"


def _record_result(result, outcome):
    if outcome == "created":
        result.created += 1
    elif outcome == "created_unknown_sender":
        result.created_unknown_sender += 1
        result.created += 1
    elif outcome == "duplicate":
        result.duplicates += 1
    elif outcome == "unknown_customer":
        result.skipped_unknown_customer += 1
    elif outcome == "missing_sender":
        result.skipped_missing_sender += 1
    else:
        result.errors += 1


def _connect():
    host = settings.INBOUND_EMAIL_HOST
    port = settings.INBOUND_EMAIL_PORT
    username = settings.INBOUND_EMAIL_USER
    password = settings.INBOUND_EMAIL_PASSWORD

    if not username or not password:
        raise ValueError("Inbound email username/password are not configured.")

    client = imaplib.IMAP4_SSL(host, port)
    client.login(username, password)
    return client


def _graph_access_token():
    tenant_id = settings.GRAPH_TENANT_ID
    client_id = settings.GRAPH_CLIENT_ID
    client_secret = settings.GRAPH_CLIENT_SECRET

    if not tenant_id or not client_id or not client_secret:
        raise ValueError("Microsoft Graph tenant/client credentials are not configured.")

    response = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _graph_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _graph_message_body(message):
    body = message.get("body") or {}
    content = body.get("content") or "(No message body)"
    if body.get("contentType") == "html":
        return html_to_text(content) or "(No message body)", content
    return content, None


def poll_graph_inbound_emails(limit=50, mark_seen=True):
    """
    Poll unread Microsoft Graph mailbox messages and create inbound communications.
    """
    result = InboundEmailPollResult()
    mailbox = settings.GRAPH_MAILBOX or settings.INBOUND_EMAIL_USER
    if not mailbox:
        raise ValueError("GRAPH_MAILBOX or INBOUND_EMAIL_USER must be configured.")

    token = _graph_access_token()
    headers = _graph_headers(token)
    params = {
        "$top": min(limit, 50),
        "$filter": "isRead eq false",
        "$orderby": "receivedDateTime desc",
        "$select": "id,internetMessageId,subject,from,toRecipients,body,receivedDateTime,isRead",
    }
    response = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/inbox/messages",
        headers=headers,
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    for message in response.json().get("value", []):
        result.scanned += 1
        try:
            from_email = (
                ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            ).lower()
            recipients = message.get("toRecipients") or []
            to_email = mailbox
            if recipients:
                to_email = ((recipients[0].get("emailAddress") or {}).get("address") or mailbox)
            content, html_content = _graph_message_body(message)
            external_id = (
                f"graph:{message.get('internetMessageId') or message.get('id')}"
            )[:255]
            outcome = _create_inbound_communication(
                sender_email=from_email,
                recipient_email=to_email,
                subject=message.get("subject") or "(No subject)",
                content=content,
                html_content=html_content,
                external_id=external_id,
            )
            _record_result(result, outcome)

            if mark_seen and outcome in {"created", "duplicate"}:
                patch_response = requests.patch(
                    f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message['id']}",
                    headers=headers,
                    json={"isRead": True},
                    timeout=30,
                )
                patch_response.raise_for_status()
        except Exception:
            logger.exception("Failed to process Microsoft Graph message %s", message.get("id"))
            result.errors += 1

    return result


def poll_inbound_emails(limit=50, mailbox=None, mark_seen=True):
    """
    Poll unread IMAP emails and create inbound Communication rows.

    Unknown senders are saved with a flag so staff can still see and reply to
    them from the global Emails page.
    """
    result = InboundEmailPollResult()
    mailbox_name = mailbox or settings.INBOUND_EMAIL_MAILBOX

    client = _connect()
    try:
        selected_status, _ = client.select(mailbox_name)
        if selected_status != "OK":
            raise ValueError(f"Unable to select inbound mailbox: {mailbox_name}")

        status, data = client.uid("search", None, "UNSEEN")
        if status != "OK":
            raise ValueError("Unable to search inbound mailbox.")

        uids = (data[0] or b"").split()
        for uid in uids[:limit]:
            result.scanned += 1
            try:
                fetch_status, fetch_data = client.uid("fetch", uid, "(RFC822)")
                if fetch_status != "OK" or not fetch_data:
                    result.errors += 1
                    continue

                raw_message = None
                for item in fetch_data:
                    if isinstance(item, tuple):
                        raw_message = item[1]
                        break
                if not raw_message:
                    result.errors += 1
                    continue

                message = email.message_from_bytes(raw_message)
                _sender_name, sender_email = parseaddr(_decode(message.get("From")))
                sender_email = sender_email.lower().strip()

                external_id = _message_external_id(message, uid.decode())
                content, html_content = _get_message_body(message)
                recipient_email = parseaddr(_decode(message.get("To")))[1] or settings.INBOUND_EMAIL_USER
                outcome = _create_inbound_communication(
                    sender_email=sender_email,
                    recipient_email=recipient_email,
                    subject=_decode(message.get("Subject")) or "(No subject)",
                    content=content,
                    html_content=html_content,
                    external_id=external_id,
                )
                _record_result(result, outcome)
                if mark_seen and outcome in {"created", "duplicate"}:
                    client.uid("store", uid, "+FLAGS", r"(\Seen)")
            except Exception:
                logger.exception("Failed to process inbound email UID %s", uid)
                result.errors += 1
    finally:
        try:
            client.close()
        except Exception:
            pass
        client.logout()

    return result


def poll_configured_inbound_emails(provider=None, limit=50, mailbox=None, mark_seen=True):
    provider_name = (provider or settings.INBOUND_EMAIL_PROVIDER).lower()
    if provider_name == "graph":
        return poll_graph_inbound_emails(limit=limit, mark_seen=mark_seen)
    if provider_name == "imap":
        return poll_inbound_emails(limit=limit, mailbox=mailbox, mark_seen=mark_seen)
    raise ValueError(f"Unsupported inbound email provider: {provider_name}")
