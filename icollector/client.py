"""Signed, backend-only client for the two scoped Mohawk Oceon endpoints."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from django.conf import settings

REJECT_PATH = '/api/partner-gateway/v1/mohawk/daily-rejects/'
DASHBOARD_PATH = '/api/partner-gateway/v1/mohawk/dashboard/main/'


class OceonError(Exception):
    def __init__(self, safe_message, http_status=None, *, retryable=True):
        self.safe_message = safe_message
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(safe_message)


class OceonClient:
    def __init__(self):
        self.base_url = settings.ICOLLECTOR_BASE_URL.rstrip('/')
        self.api_key = settings.ICOLLECTOR_API_KEY
        self.secret = settings.ICOLLECTOR_HMAC_SECRET
        origin = urlsplit(self.base_url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username
                or origin.password or origin.path or origin.query or origin.fragment
                or not self.api_key or not self.secret):
            raise OceonError('Configure the Oceon HTTPS origin and backend credentials.')

    def request(self, method, path, payload=None):
        if (method, path) not in (('POST', REJECT_PATH), ('GET', DASHBOARD_PATH)):
            raise OceonError('Unsupported Oceon operation.', retryable=False)
        raw = b'' if payload is None else json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
        headers = signed_headers(self.api_key, self.secret, method, path, raw)
        session = requests.Session()
        session.trust_env = False
        proxy = settings.ICOLLECTOR_PROXY_URL
        if proxy:
            session.proxies = {'https': proxy}
        try:
            with session, session.request(method, self.base_url + path, data=raw, headers=headers,
                    timeout=(5, 20), allow_redirects=False, stream=True) as response:
                status = response.status_code
                if not 200 <= status < 300:
                    messages = {
                        400: 'Oceon rejected the row fields; review the source data before retrying.',
                        401: 'Oceon authentication failed; check credentials, signature and server time.',
                        403: 'Oceon denied the source IP or tenant.',
                        404: 'Oceon endpoint was not found; verify that its integration is deployed.',
                        409: 'Oceon rejected a reused nonce; the next retry will use a new signature.',
                        429: 'Oceon rate limit reached; delivery will retry.',
                        503: 'Oceon integration is disabled or unavailable.',
                    }
                    raise OceonError(messages.get(status, f'Oceon returned HTTP {status}.'),
                        status, retryable=status not in (400, 413, 422))
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise OceonError('Oceon response exceeded the supported size.', status)
                    chunks.append(chunk)
                try:
                    result = json.loads(b''.join(chunks))
                except (ValueError, UnicodeDecodeError):
                    raise OceonError('Oceon returned an invalid JSON response.', status) from None
                if not isinstance(result, dict):
                    raise OceonError('Oceon returned an unexpected response format.', status)
                return status, result
        except requests.RequestException:
            # Never include requests exceptions: they can contain proxy credentials.
            raise OceonError('Oceon connection failed or timed out; delivery will retry.') from None

    def send_reject(self, payload):
        status, data = self.request('POST', REJECT_PATH, payload)
        row = data.get('row')
        if not isinstance(row, dict) or row.get('id') is None:
            raise OceonError('Oceon did not acknowledge a Daily Reject row ID.', status)
        return {'http_status': status, 'row_id': str(row['id'])[:200],
                'idempotent_replay': data.get('idempotent_replay') is True}

    def dashboard(self):
        status, data = self.request('GET', DASHBOARD_PATH)
        if not isinstance(data.get('dashboard'), dict) or not isinstance(data.get('widgets'), list):
            raise OceonError('Oceon returned an unexpected dashboard schema.', status)
        return data


def signed_headers(api_key, secret, method, path, raw, *, timestamp=None, nonce=None):
    timestamp = timestamp or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    nonce = nonce or secrets.token_urlsafe(18)
    canonical = f'{timestamp}.{nonce}.{method.upper()}.{path}.{hashlib.sha256(raw).hexdigest()}'
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json',
        'X-Partner-Timestamp': timestamp, 'X-Partner-Nonce': nonce,
        'X-Partner-Signature': f'sha256={signature}'}
