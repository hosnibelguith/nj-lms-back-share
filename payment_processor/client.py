"""Independent ZUM transport. No LMS payment services or undocumented portal APIs."""
import hashlib
import re
import time
from urllib.parse import urlparse

import requests
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from .access import require_enabled, writes_enabled
from .models import RequestBudget


class ProviderError(Exception):
    def __init__(self, message, *, status=502, retry_after=0, unknown=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.unknown = unknown


def integration_setting(key, default=''):
    # Host adapter: this is the only dependency on the LMS configuration model.
    if apps.is_installed('accounts'):
        model = apps.get_model('accounts', 'GlobalSetting')
        value = model.objects.filter(key=key).values_list('value', flat=True).first()
        if value is not None:
            return value
    return getattr(settings, key, default)


def sanitize(value):
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        normalized = re.sub('[^a-z]', '', key.lower())
        if any(word in normalized for word in ('password', 'apikey', 'secret', 'securityanswer', 'securityquestion', 'token', 'cvv', 'cvc')) or normalized in ('pin', 'pan', 'username', 'authcode', 'bytes', 'idnumber', 'ssn', 'socialsecuritynumber', 'cardpin'):
            continue
        if normalized in ('accountnumber', 'bankaccountnumber', 'cardnumber', 'creditcardnumber', 'number', 'securitydepositcardnumber'):
            result[key] = ('****' + str(item)[-4:]) if item else None
        else:
            result[key] = sanitize(item)
    return result


def unwrap(data):
    return data['result'] if isinstance(data, dict) and 'result' in data else data


def bank_payload(value):
    """Hide unsupported product fields in both existing snapshots and live reads."""
    value = sanitize(value)
    if isinstance(value, list):
        return [bank_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        normalized = re.sub('[^a-z]', '', key.casefold())
        if any(product in normalized for product in ('card', 'invoice', 'subscription')):
            continue
        if normalized == 'transactionmethodsavailabledescription':
            if isinstance(item, str):
                item = ', '.join(method.strip() for method in item.split(',')
                                 if method.strip().casefold() in ('eft', 'interac', 'ach'))
            elif isinstance(item, list):
                item = [method for method in item if str(method).casefold() in ('eft', 'interac', 'ach')]
        result[key] = bank_payload(item)
    return result


def page_result(data):
    data = unwrap(data)
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and 'Items' in data[0]:
        data = data[0]
    if isinstance(data, dict):
        for name in ('WalletTransactions', 'Transactions', 'Users', 'FundingSources'):
            if isinstance(data.get(name), dict) and 'Items' in data[name]:
                data = data[name]
                break
    if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
        raise ProviderError('ZUM returned an unrecognized paginated response; no report was published.')
    for name in ('TotalCount', 'CurrentPage'):
        if not isinstance(data.get(name), int) or data[name] < (1 if name == 'CurrentPage' else 0):
            raise ProviderError('ZUM did not return valid pagination metadata.')
    if any(not isinstance(item, dict) or not item.get('Id') for item in data['Items']):
        raise ProviderError('ZUM returned an activity item without an identifier.')
    return data


class ZumClient:
    READ_POSTS = {'/transaction/filter', '/user/filter', '/fundingsource/filter', '/wallet/transactions/filter'}
    READ_GET = re.compile(r'^/(wallet|transaction/[\w-]+|user/[\w-]+|fundingsource/[\w-]+)$')

    def __init__(self):
        self.base = str(integration_setting('ZUMRAILS_API_BASE_URL')).rstrip('/')
        self.username = str(integration_setting('ZUMRAILS_USERNAME')).strip()
        self.password = str(integration_setting('ZUMRAILS_PASSWORD'))
        parsed = urlparse(self.base)
        if not self.username or not self.password or parsed.scheme != 'https' or parsed.username or parsed.query or parsed.fragment or not (parsed.hostname or '').endswith(('.zumrails.com', '.zumrails.app')):
            raise ProviderError('Configure a valid ZUM HTTPS URL and credentials in API Integrations.', status=503)
        self.endpoint = self.base if self.base.lower().endswith('/api') else self.base + '/api'
        # Tenant and credential identity isolate every snapshot, token and operation.
        try:
            from config.tenant_context import get_current_tenant_database
            tenant = get_current_tenant_database()
        except ImportError:
            tenant = 'default'
        self.scope = hashlib.sha256(f'{tenant}:{self.base}:{self.username}'.encode()).hexdigest()
        self.token_key = 'payment-processor:token:' + hashlib.sha256(f'{self.scope}:{self.password}'.encode()).hexdigest()

    def reserve(self, bucket='read'):
        limit = 75 if bucket == 'filter' else 200
        now = time.time()
        key = f'{self.scope}:{bucket}'
        RequestBudget.objects.using('default').get_or_create(key=key)
        with transaction.atomic(using='default'):
            row = RequestBudget.objects.using('default').select_for_update().get(pk=key)
            times = [stamp for stamp in row.timestamps if stamp > now - 300]
            if len(times) >= limit:
                raise ProviderError('ZUM read budget reached. Synchronization will resume automatically.', status=429, retry_after=max(1, int(times[0] + 301 - now)))
            row.timestamps = times + [now]
            row.save(using='default', update_fields=['timestamps'])

    def token(self, force=False):
        token = None if force else cache.get(self.token_key)
        if token:
            return token
        self.reserve('auth')
        data = self.transport('POST', '/authorize', {'Username': self.username, 'Password': self.password}, {})
        result = unwrap(data)
        token = result.get('Token') or result.get('token') if isinstance(result, dict) else None
        if not token:
            raise ProviderError('ZUM authentication did not return a token.')
        cache.set(self.token_key, token, 50 * 60)
        return token

    def transport(self, method, path, payload, headers, mutation=False):
        try:
            response = requests.request(method, self.endpoint + path, json=payload, headers={'Accept': 'application/json', **headers}, timeout=(10, 35), allow_redirects=False)
        except requests.RequestException:
            raise ProviderError('ZUM did not respond. Retry a read; review an uncertain action before retrying.', unknown=mutation) from None
        if not 200 <= response.status_code < 300:
            retry = response.headers.get('Retry-After', '60')
            raise ProviderError(f'ZUM returned HTTP {response.status_code}.', status=response.status_code if response.status_code in (401, 403, 404, 429) else 502,
                                retry_after=min(600, int(retry)) if retry.isdigit() and response.status_code == 429 else 0,
                                unknown=mutation and (response.status_code >= 500 or response.status_code in (408, 409, 429)))
        if response.status_code == 204:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise ProviderError('ZUM returned invalid JSON.', unknown=mutation) from None
        if isinstance(data, dict) and data.get('isError'):
            raise ProviderError('ZUM rejected this request. Verify the selected action and account capabilities.', status=422)
        return data

    def request(self, method, path, payload=None, *, operation_id=None):
        require_enabled()
        read = (method == 'GET' and self.READ_GET.fullmatch(path)) or (method == 'POST' and path in self.READ_POSTS)
        if not read and (not operation_id or not writes_enabled()):
            raise ProviderError('Live actions are disabled. Enable provider writes in the Payment processor admin switch and server configuration.', status=403)
        self.reserve('filter' if path.endswith('/filter') else 'read')
        headers = {'Authorization': 'Bearer ' + self.token()}
        if operation_id:
            headers['idempotency-key'] = str(operation_id)
        try:
            return self.transport(method, path, payload, headers, mutation=not read)
        except ProviderError as error:
            # Only reads retry automatically. Never replay an uncertain mutation.
            if error.status != 401 or not read:
                raise
            headers['Authorization'] = 'Bearer ' + self.token(force=True)
            return self.transport(method, path, payload, headers)
