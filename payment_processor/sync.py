"""Durable, restartable reads. Only a fully counted snapshot becomes reportable."""
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .access import require_enabled
from .client import ProviderError, ZumClient, page_result, sanitize, unwrap
from .models import DetailCache, PluginSettings, SyncRow, SyncRun, WebhookInbox

RESOURCES = {'transactions': '/transaction/filter', 'users': '/user/filter',
             'funding_sources': '/fundingsource/filter', 'ledger': '/wallet/transactions/filter'}
CORE_RESOURCES = ('transactions', 'users', 'funding_sources', 'ledger')


def as_date(value):
    if not value:
        return None
    try:
        date = parse_datetime(str(value))
    except ValueError:
        return None
    if date and timezone.is_naive(date):
        date = timezone.make_aware(date, dt_timezone.utc)
    return date


def row_values(payload, resource):
    data = sanitize(payload)
    raw_amount = data.get('Amount')
    amount = None
    if resource == 'transactions':
        try:
            amount = Decimal(str(raw_amount))
            if not amount.is_finite() or amount != amount.quantize(Decimal('.01')):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ProviderError('A ZUM transaction has an invalid amount; this snapshot cannot be used for reports.')
    user = data.get('User') if isinstance(data.get('User'), dict) else {}
    wallet = data.get('Wallet') if isinstance(data.get('Wallet'), dict) else {}
    failure = data.get('FailedTransactionEvent')
    if not failure and data.get('TransactionStatus') in ('Failed', 'Rejected'):
        events = [event for event in (data.get('TransactionHistory') or data.get('EventHistory') or []) if isinstance(event, dict)]
        failures = [event for event in events if any(term in str(event.get('Event', '')).casefold() for term in ('failed', 'declined', 'rejected'))]
        failure = str(sorted(failures, key=lambda event: str(event.get('CreatedAt', '')))[-1].get('Event')) if failures else 'Not provided by ZUM'
    return {'payload': data, 'created_at': as_date(data.get('CreatedAt')), 'amount': amount,
            'currency': data.get('Currency') or wallet.get('Currency') or '',
            'flow': data.get('ZumRailsType') or '', 'method': data.get('TransactionMethod') or '',
            'status': data.get('TransactionStatus') or '', 'failure': str(failure or '')[:255],
            'user_id': str(user.get('Id') or data.get('UserId') or ''),
            'user_name': str(user.get('Name') or ' '.join(filter(None, [user.get('FirstName'), user.get('LastName')])) or data.get('UserName') or '')[:255],
            'completed_at': as_date(data.get('CompletedAt'))}


def cached(client, resource, provider_id, fetch, ttl=30, force=False):
    row = DetailCache.objects.using('default').filter(scope=client.scope, resource=resource, provider_id=provider_id).first()
    if row and not force and row.synced_at > timezone.now() - timedelta(seconds=ttl):
        return row.payload, row.synced_at
    value = sanitize(fetch())
    now = timezone.now()
    DetailCache.objects.using('default').update_or_create(scope=client.scope, resource=resource, provider_id=provider_id,
                                                       defaults={'payload': value, 'synced_at': now})
    return value, now


def wallets(client, force=False):
    def fetch():
        value = unwrap(client.request('GET', '/wallet'))
        if not isinstance(value, list) or any(not isinstance(w, dict) or not w.get('Id') for w in value):
            raise ProviderError('ZUM returned an invalid wallet list.')
        for wallet in value:
            try:
                balance = Decimal(str(wallet['Balance']))
                if not balance.is_finite() or not wallet.get('Currency'):
                    raise ValueError
                wallet['Balance'] = str(balance)
            except (KeyError, InvalidOperation, ValueError):
                raise ProviderError('ZUM did not return a valid wallet balance and currency.')
        # Provider Customer contains unrelated configuration and API keys.
        return [{key: w.get(key) for key in ('Id', 'Type', 'Currency', 'Balance')} for w in value]
    return cached(client, 'wallets', 'all', fetch, ttl=15, force=force)


def selected_wallet(client):
    from .client import integration_setting
    values, _ = wallets(client)
    selected = str(integration_setting('ZUMRAILS_WALLET_ID', '')).strip()
    matches = [w for w in values if str(w['Id']) == selected] if selected else [w for w in values if w.get('Currency') == 'CAD' and w.get('Type') in ('Unified', 'AccountsPayable')]
    if len(matches) != 1:
        raise ProviderError('Select a unique ZUM wallet in API Integrations.')
    return matches[0]


def latest_run(scope, resource):
    return SyncRun.objects.using('default').filter(scope=scope, resource=resource, status='complete').order_by('-completed_at').first()


def queue_sync(client, resources=CORE_RESOURCES):
    require_enabled()
    runs = []
    resources = list(resources)
    with transaction.atomic(using='default'):
        PluginSettings.objects.using('default').select_for_update().get(pk=1)
        for resource in resources:
            if resource not in RESOURCES:
                raise ProviderError('Unknown synchronization resource.', status=400)
            run = SyncRun.objects.using('default').filter(scope=client.scope, resource=resource, status__in=['queued', 'running']).first()
            if run is None:
                run = SyncRun.objects.using('default').create(scope=client.scope, resource=resource)
            runs.append(run)
    return runs


def sync_page(client, run):
    require_enabled()
    if run.scope != client.scope or run.status not in ('queued', 'running'):
        return
    if run.resource not in RESOURCES:
        run.status = 'cancelled'
        run.error = 'This section has been removed.'
        run.save(using='default', update_fields=['status', 'error'])
        return
    now = timezone.now()
    if run.retry_at and run.retry_at > now:
        return
    run.status = 'running'
    run.heartbeat_at = now
    run.save(using='default', update_fields=['status', 'heartbeat_at'])
    payload = {'Pagination': {'PageNumber': run.next_page, 'ItemsPerPage': 100}}
    # Freeze the creation boundary so newly-created rows do not shift every page.
    if run.resource in ('transactions', 'users', 'ledger'):
        payload.update(CreatedAtOperator='IsBetween', CreatedAtFrom='1970-01-01T00:00:00Z',
                       CreatedAtTo=run.started_at.isoformat(), TimeZone='UTC')
    if run.resource == 'ledger':
        try:
            payload['WalletId'] = selected_wallet(client)['Id']
        except ProviderError as error:
            run.error = str(error)[:250]
            run.status = 'running' if error.status == 429 else 'failed'
            run.retry_at = timezone.now() + timedelta(seconds=error.retry_after or 60) if error.status == 429 else None
            run.save(using='default', update_fields=['error', 'status', 'retry_at'])
            return
    try:
        page = page_result(client.request('POST', RESOURCES[run.resource], payload))
        if page['CurrentPage'] != run.next_page:
            raise ProviderError('ZUM returned an unexpected page; the previous complete report remains available.')
        if run.expected_count is not None and page['TotalCount'] != run.expected_count:
            raise ProviderError('ZUM result count changed during synchronization. Start a new synchronization to get a complete report.')
        existing = set(SyncRow.objects.using('default').filter(run=run).values_list('provider_id', flat=True))
        ids = [str(item['Id']) for item in page['Items']]
        if len(ids) != len(set(ids)) or existing.intersection(ids):
            raise ProviderError('ZUM returned duplicate pages. This incomplete snapshot was not published.')
        if not ids and run.fetched_count != page['TotalCount']:
            raise ProviderError('ZUM returned an empty page before all transactions were received.')
        records = [SyncRow(run=run, provider_id=str(item['Id']), **row_values(item, run.resource)) for item in page['Items']]
        # Currency may be omitted on Canadian transaction filters. Only infer it
        # when the authenticated account has exactly one wallet currency.
        if run.resource == 'transactions' and any(not row.currency for row in records):
            wallet_values, _ = wallets(client)
            currencies = {w['Currency'] for w in wallet_values if w.get('Currency')}
            for row in records:
                if not row.currency:
                    row.currency = next(iter(currencies)) if len(currencies) == 1 else 'UNKNOWN'
                    row.payload['Currency'] = row.currency
                    row.payload['CurrencySource'] = 'account_wallets' if len(currencies) == 1 else 'unavailable'
        with transaction.atomic(using='default'):
            SyncRow.objects.using('default').bulk_create(records)
            run.expected_count = page['TotalCount']
            run.fetched_count += len(ids)
            run.next_page += 1
            run.retry_at = None
            run.error = ''
            if run.fetched_count > run.expected_count:
                raise ProviderError('ZUM returned more rows than its advertised count.')
            if run.fetched_count == run.expected_count:
                run.status = 'complete'
                run.completed_at = timezone.now()
            run.save(using='default')
        if run.status == 'complete':
            # Keep two complete snapshots for comparison/rollback, not a new
            # permanent copy of every transaction every five minutes.
            keep = list(SyncRun.objects.using('default').filter(scope=client.scope, resource=run.resource, status='complete').order_by('-completed_at').values_list('pk', flat=True)[:2])
            SyncRun.objects.using('default').filter(scope=client.scope, resource=run.resource, status='complete').exclude(pk__in=keep).delete()
    except ProviderError as error:
        run.error = str(error)[:250]
        if error.status == 429:
            run.retry_at = timezone.now() + timedelta(seconds=error.retry_after or 60)
        else:
            run.status = 'failed'
        run.save(using='default', update_fields=['status', 'error', 'retry_at'])


def transaction_detail(client, provider_id, force=False):
    def fetch():
        data = unwrap(client.request('GET', '/transaction/' + provider_id))
        if not isinstance(data, dict) or str(data.get('Id')) != provider_id:
            raise ProviderError('ZUM returned an unexpected transaction.')
        return data
    return cached(client, 'transaction', provider_id, fetch, ttl=15, force=force)


def mirror_existing_webhooks(client):
    config = require_enabled()
    if not config.read_existing_webhooks or not apps.is_installed('loans'):
        return 0
    model = apps.get_model('loans', 'WebhookEvent')
    marker, _ = DetailCache.objects.using('default').get_or_create(scope=client.scope, resource='webhook_cursor', provider_id='lms', defaults={'payload': {}})
    since = as_date(marker.payload.get('received_at')) or timezone.now() - timedelta(days=1)
    # Read-only bridge. A small overlap makes equal timestamps and retries safe.
    events = model.objects.filter(received_at__gte=since - timedelta(seconds=1)).order_by('received_at')[:1000]
    count = 0
    high_water = since
    for event in events:
        data = sanitize(event.payload)
        _, added = WebhookInbox.objects.using('default').get_or_create(scope=client.scope, digest=event.payload_hash,
            defaults={'payload': data, 'source': 'lms', 'received_at': event.received_at})
        count += int(added)
        high_water = max(high_water, event.received_at)
    marker.payload = {'received_at': high_water.isoformat()}
    marker.save(using='default', update_fields=['payload'])
    return count


def process_webhooks(client):
    now = timezone.now()
    events = WebhookInbox.objects.using('default').filter(scope=client.scope, processed_at__isnull=True).order_by('received_at')[:20]
    for event in events:
        if event.retry_at and event.retry_at > now:
            continue
        data = event.payload.get('Data') or event.payload.get('data') or {}
        try:
            if event.payload.get('Type', '').casefold() == 'transaction':
                import uuid
                provider_id = str(uuid.UUID(str(data.get('Id') or data.get('TransactionId'))))
                transaction_detail(client, provider_id, force=True)
                # Report publication still requires a full counted snapshot.
                queue_sync(client, ('transactions',))
            event.processed_at = timezone.now()
            event.error = ''
        except (ProviderError, ValueError, TypeError) as error:
            event.error = str(error)[:250] if isinstance(error, ProviderError) else 'Webhook identifier is invalid.'
            event.retry_at = now + timedelta(seconds=min(3600, 30 * 2 ** min(event.attempts, 6)))
        event.attempts += 1
        event.save(using='default')


def run_cycle(client):
    config = require_enabled()
    mirror_existing_webhooks(client)
    process_webhooks(client)
    for resource in CORE_RESOURCES:
        latest = latest_run(client.scope, resource)
        last_attempt = SyncRun.objects.using('default').filter(scope=client.scope, resource=resource).order_by('-started_at').first()
        interval = max(2, config.reconcile_minutes) * 60
        due = not latest or latest.completed_at < timezone.now() - timedelta(seconds=interval)
        if due and (not last_attempt or last_attempt.status != 'failed' or last_attempt.started_at < timezone.now() - timedelta(seconds=interval)):
            queue_sync(client, (resource,))
    run = SyncRun.objects.using('default').filter(scope=client.scope, resource__in=RESOURCES, status__in=['queued', 'running']).order_by('started_at').first()
    if run:
        sync_page(client, run)
    return run
