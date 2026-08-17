import logging
import time

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils.timezone import now

from .constants import (
    UNSUPPORTED_IBV_INSTITUTIONS,
    is_payment_blocked_institution,
    normalize_institution_number as _normalize_institution_number,
)
from .logging_utils import mask_identifier
from .models import BankConnection, BankAccount, BankTransaction

logger = logging.getLogger(__name__)

FLINKS_ASYNC_POLL_INTERVAL_SECONDS = 10
FLINKS_ASYNC_MAX_WAIT_SECONDS = 30 * 60
# Live Authorize first (not cache) — cached pulls often return 0 txs for
# newly linked KOHO / neo-banks while Flinks toolbox already shows history.
# Cache is only a fallback after timeout / MFA so we do not fail IBV.
FLINKS_MOST_RECENT_CACHED = False
FLINKS_HTTP_TIMEOUT = 60
FLINKS_ZERO_TX_RETRIES = 3
FLINKS_ZERO_TX_RETRY_SECONDS = 5
# Leave IBV pending (do not fail-email) while GAD can still succeed later.
# The 5-minute beat re-runs the same pull as the staff Re-pull IBV button.
FLINKS_GAD_AUTOMATED_MAX_ATTEMPTS = 36
FLINKS_TRANSIENT_CODES = frozenset({
    'OPERATION_PENDING',
    'OPERATION_DISPATCHED',
    'CARD_IN_USE',
    'RETRY_LATER',
    'CONCURRENT_SESSION',
})
FLINKS_TERMINAL_CODES = frozenset({
    'INVALID_LOGIN',
    'INVALID_PASSWORD',
    'INVALID_USERNAME',
    'DISABLED_LOGIN',
    'DISABLED_INSTITUTION',
    'NEW_ACCOUNT',
    'INVALID_SECURITY_RESPONSE',
    'INVALID_SECURITY_RESPONSE_NO_RETRY',
})
FLINKS_GAD_SAFETY_NET_MIN_AGE_SECONDS = 120
# Do not steal a live GetAccountsDetailAsync poll (up to 30 minutes).
FLINKS_GAD_STALE_SYNCING_SECONDS = FLINKS_ASYNC_MAX_WAIT_SECONDS + 120
# Sequential inline GAD; keep one beat tick shorter than the 5-minute schedule.
FLINKS_GAD_SAFETY_NET_BATCH_SIZE = 4
ZERO_TRANSACTIONS_MESSAGE = (
    'We could not retrieve transaction history from your bank account. '
    'Please reconnect your bank account.'
)
NO_ACCOUNTS_MESSAGE = (
    'No bank accounts were returned. Please reconnect your bank account.'
)
UNSUPPORTED_INSTITUTION_MESSAGE = (
    'We could not complete banking verification with this financial institution. '
    'Please reconnect using a supported bank account.'
)
UNSUPPORTED_IBV_REASON_CODE = 'unsupported_ibv_institution'


def _normalize_account_type(raw_type):
    value = (raw_type or 'other').strip().lower()
    if value in {'chequing', 'checking'}:
        return 'checking'
    if value in {'savings', 'saving'}:
        return 'savings'
    if value in {'credit', 'loan', 'investment'}:
        return value
    return 'other'


def _unsupported_institutions(accounts_data):
    institutions = set()
    for account in accounts_data:
        institution = _normalize_institution_number(account.get('InstitutionNumber'))
        if institution in UNSUPPORTED_IBV_INSTITUTIONS:
            institutions.add(institution)
    return institutions


def _flinks_headers(secret_key):
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'flinks-auth-key': secret_key,
    }


def _poll_get_accounts_detail_async(instance, customer_id, request_id, headers):
    """
    Poll /GetAccountsDetailAsync after /GetAccountsDetail returns 202.
    See: https://docs.flinks.com/api/connect/endpoints/account-linking/get-accounts-detail-async
    """
    async_url = (
        f'https://{instance}-api.private.fin.ag/v3/{customer_id}/'
        f'BankingServices/GetAccountsDetailAsync/{request_id}'
    )
    deadline = time.monotonic() + FLINKS_ASYNC_MAX_WAIT_SECONDS
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        async_resp = requests.get(async_url, headers=headers, timeout=FLINKS_HTTP_TIMEOUT)
        logger.info(
            'Flinks async poll attempt=%s request_id=%s status=%s',
            attempt,
            mask_identifier(request_id),
            async_resp.status_code,
        )
        if async_resp.status_code == 200:
            logger.info(
                'Flinks async poll completed request_id=%s attempts=%s',
                mask_identifier(request_id),
                attempt,
            )
            return async_resp.json()
        if async_resp.status_code == 202:
            time.sleep(FLINKS_ASYNC_POLL_INTERVAL_SECONDS)
            continue
        logger.error(
            'Flinks GetAccountsDetailAsync failed request_id=%s status=%s body=%s',
            mask_identifier(request_id),
            async_resp.status_code,
            async_resp.text,
        )
        return None

    logger.error(
        'Flinks GetAccountsDetailAsync timed out request_id=%s max_wait_seconds=%s',
        mask_identifier(request_id),
        FLINKS_ASYNC_MAX_WAIT_SECONDS,
    )
    return None


def _extract_holder_identity(accounts_data):
    flinks_email = None
    flinks_phone = None
    flinks_name = None

    for acc in accounts_data:
        holder = acc.get('Holder') or {}
        if not flinks_name and holder.get('Name'):
            flinks_name = holder.get('Name')
        if not flinks_email and holder.get('Email'):
            flinks_email = holder.get('Email')
        if not flinks_phone and holder.get('PhoneNumber'):
            flinks_phone = holder.get('PhoneNumber')
        if flinks_email and flinks_phone and flinks_name:
            break

    return flinks_email, flinks_phone, flinks_name


def _count_transactions(accounts_data):
    total = 0
    for acc in accounts_data:
        transactions = acc.get('Transactions') or []
        total += len(transactions)
    return total


def _log_banking_failure(customer, title, description, metadata=None):
    try:
        from activity.models import ActivityHistory

        activity_metadata = {'source': 'flinks_sync'}
        if metadata:
            activity_metadata.update(metadata)

        ActivityHistory.objects.create(
            customer=customer,
            type='system',
            title=title,
            description=description,
            created_by='system',
            metadata=activity_metadata,
        )
    except Exception:
        logger.exception('Failed to log banking failure activity for customer=%s', customer.id)


def _mark_banking_failed(connection, customer, reason):
    logger.warning(
        'Flinks sync marking failed customer_id=%s connection_id=%s login_id=%s reason=%s',
        customer.id,
        connection.id,
        mask_identifier(connection.login_id),
        reason,
    )
    connection.sync_status = 'failed'
    connection.sync_error = reason
    connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

    customer.banking_verified = False
    if customer.onboarding_stage != 'banking_verification':
        customer.onboarding_stage = 'banking_verification'
    customer.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])

    _log_banking_failure(customer, 'Banking Verification Failed', reason)
    send_banking_retry_email.delay(str(customer.id), reason)
    logger.warning('Flinks sync failed customer_id=%s connection_id=%s', customer.id, connection.id)
    return False


def _is_transient_flinks_transport_error(exc) -> bool:
    """Network / read timeouts — Flinks may still deliver GetAccountsDetail via webhook."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    text = str(exc).lower()
    return 'timed out' in text or 'timeout' in text or 'connection reset' in text


def _extract_flinks_code(payload) -> str:
    if not isinstance(payload, dict):
        return ''
    for key in ('FlinksCode', 'flinksCode', 'Code'):
        value = payload.get(key)
        if value:
            return str(value).strip().upper()
    return ''


def _flinks_code_from_response(response) -> str:
    if response is None:
        return ''
    try:
        body = response.json()
    except Exception:
        return ''
    return _extract_flinks_code(body)


def _is_transient_flinks_api_error(response) -> bool:
    """Flinks codes that mean the LoginId is valid but aggregation is still running."""
    if response is None:
        return False
    if getattr(response, 'status_code', None) == 202:
        return True
    return _flinks_code_from_response(response) in FLINKS_TRANSIENT_CODES


def _is_mfa_awaiting_error(sync_error: str) -> bool:
    """203 / SecurityChallenges cannot be completed by a server-side GAD re-pull."""
    text = sync_error or ''
    compact = text.lower().replace('_', '').replace(' ', '')
    return 'securitychallenge' in compact or 'security challenge' in text.lower()


def _is_terminal_flinks_failure(reason: str) -> bool:
    """Credential / institution errors — restarting GAD cannot create a LoginId."""
    text = (reason or '').upper()
    return any(code in text for code in FLINKS_TERMINAL_CODES)


def should_schedule_gad_repull(connection) -> bool:
    if connection.provider != 'flinks':
        return False
    if not str(connection.login_id or '').strip():
        return False
    customer = getattr(connection, 'customer', None)
    if customer is not None and customer.banking_verified and connection.sync_status == 'synced':
        return False
    if connection.last_synced_at is not None and connection.sync_status == 'synced':
        return False
    if _is_mfa_awaiting_error(connection.sync_error or ''):
        return False
    if _is_terminal_flinks_failure(connection.sync_error or ''):
        return False
    return (connection.attempted_syncs or 0) < FLINKS_GAD_AUTOMATED_MAX_ATTEMPTS


def unsynced_ibv_safety_net_targets(*, now_value=None):
    """Pending IBV rows that still have a Flinks LoginId.

    Includes ibv_pending loans (Arrive and landing). Skips MFA / INVALID_LOGIN
    and a live GetAccountsDetailAsync poll. Rows with no LoginId cannot GAD.
    """
    from datetime import timedelta

    now_value = now_value or now()
    updated_before = now_value - timedelta(seconds=FLINKS_GAD_SAFETY_NET_MIN_AGE_SECONDS)
    stale_syncing_before = now_value - timedelta(seconds=FLINKS_GAD_STALE_SYNCING_SECONDS)

    targets = []
    for connection in pending_ibv_repull_targets(include_syncing=True):
        if not str(connection.login_id or '').strip():
            continue
        if connection.last_synced_at is not None and connection.sync_status == 'synced':
            continue
        if connection.sync_status == 'syncing':
            if not connection.updated_at or connection.updated_at > stale_syncing_before:
                continue
        elif connection.updated_at and connection.updated_at > updated_before:
            continue
        if not should_schedule_gad_repull(connection):
            continue
        targets.append(connection)
    targets.sort(key=lambda row: row.updated_at or row.created_at)
    return targets


def _mark_awaiting_flinks_webhook(connection, reason: str) -> None:
    """Keep IBV open after a pull timeout so webhook / Mohawk can complete it."""
    message = (
        f'Flinks pull timed out; awaiting GetAccountsDetail webhook. ({reason})'
    )[:2000]
    connection.sync_status = 'pending'
    connection.sync_error = message
    connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
    logger.warning(
        'Flinks sync awaiting webhook customer_id=%s connection_id=%s login_id=%s reason=%s',
        connection.customer_id,
        connection.id,
        mask_identifier(connection.login_id),
        reason,
    )


def _connection_has_transactions(connection) -> bool:
    return BankTransaction.objects.filter(account__connection=connection).exists()


def _try_complete_ibv_from_mohawk_event(connection) -> bool:
    """If Mohawk already posted analysis for this login, finish IBV without Flinks pull."""
    from banking.models import BankingAnalysisEvent
    from banking.webhooks import (
        _apply_primary_eft_account,
        _complete_ibv_from_mohawk_analysis,
        _coords_complete,
    )

    event = (
        BankingAnalysisEvent.objects.filter(login_id=str(connection.login_id or ''))
        .order_by('-received_at')
        .first()
    )
    if event is None:
        return False

    primary = event.primary_bank_account if isinstance(event.primary_bank_account, dict) else {}
    txs = event.source_transactions if isinstance(event.source_transactions, list) else []
    if _coords_complete(primary):
        _apply_primary_eft_account(connection, primary)
    return _complete_ibv_from_mohawk_analysis(
        connection,
        primary=primary,
        source_transactions=txs,
    )


def _is_flinks_security_challenge(response) -> bool:
    """Authorize 203 / SecurityChallenges = MFA, not a completed IBV failure."""
    if response is None:
        return False
    if getattr(response, 'status_code', None) == 203:
        return True
    try:
        body = response.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    return bool(body.get('SecurityChallenges')) or body.get('HttpStatusCode') == 203


def _try_cached_flinks_detail(connection, instance, flinks_customer_id, headers, login_id) -> bool:
    """Use data Flinks already stored from Connect when a live pull cannot finish."""
    auth_url = (
        f'https://{instance}-api.private.fin.ag/v3/{flinks_customer_id}/'
        'BankingServices/Authorize'
    )
    acct_url = (
        f'https://{instance}-api.private.fin.ag/v3/{flinks_customer_id}/'
        'BankingServices/GetAccountsDetail'
    )
    try:
        auth_resp = requests.post(
            auth_url,
            json={'LoginId': str(login_id), 'MostRecentCached': True},
            headers=headers,
            timeout=FLINKS_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception(
            'Flinks cached Authorize failed connection_id=%s',
            connection.id,
        )
        return False
    if auth_resp.status_code != 200:
        return False
    request_id = (auth_resp.json() or {}).get('RequestId')
    if not request_id:
        return False
    try:
        acct_resp = requests.post(
            acct_url,
            json={'RequestId': request_id, 'DaysOfTransactions': 'Days365'},
            headers=headers,
            timeout=FLINKS_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception(
            'Flinks cached GetAccountsDetail failed connection_id=%s',
            connection.id,
        )
        return False
    if acct_resp.status_code != 200:
        return False
    payload = acct_resp.json() or {}
    if _count_transactions(payload.get('Accounts') or []) == 0:
        return False
    logger.info(
        'Flinks cached pull recovered IBV connection_id=%s customer_id=%s',
        connection.id,
        connection.customer_id,
    )
    return apply_flinks_accounts_detail(connection, payload)


def _recover_ibv_without_live_pull(
    task,
    connection,
    customer,
    reason,
    *,
    instance,
    flinks_customer_id,
    headers,
    login_id,
):
    """Do not fail IBV when live Authorize timed out or asked for MFA."""
    if _try_complete_ibv_from_mohawk_event(connection):
        return True
    if connection.accounts.exists() and _connection_has_transactions(connection):
        return _mark_banking_success(connection, customer)
    if _try_cached_flinks_detail(
        connection, instance, flinks_customer_id, headers, login_id
    ):
        return True

    _mark_awaiting_flinks_webhook(connection, reason)
    # Do not apply_async/delay from this worker. Redis often accepts the
    # message (activity "IBV Re-pull Started") and never runs GAD. The beat
    # task executes the same pull as the staff button via apply().
    return False


def _handle_flinks_transport_error(
    task,
    connection,
    customer,
    exc,
    *,
    instance,
    flinks_customer_id,
    headers,
    login_id,
):
    """Prefer cache / webhook / Mohawk over hard-failing IBV on Flinks timeouts."""
    if not _is_transient_flinks_transport_error(exc):
        return _mark_banking_failed(connection, customer, str(exc))
    return _recover_ibv_without_live_pull(
        task,
        connection,
        customer,
        str(exc),
        instance=instance,
        flinks_customer_id=flinks_customer_id,
        headers=headers,
        login_id=login_id,
    )


def apply_flinks_accounts_detail(connection, accounts_json) -> bool:
    """Persist a GetAccountsDetail-shaped payload (API pull or Flinks webhook)."""
    customer = connection.customer
    accounts_data = (accounts_json or {}).get('Accounts') or []
    if not accounts_data:
        return _mark_banking_failed(connection, customer, NO_ACCOUNTS_MESSAGE)

    if _count_transactions(accounts_data) == 0:
        return _mark_banking_failed(connection, customer, ZERO_TRANSACTIONS_MESSAGE)

    flinks_email, flinks_phone, flinks_name = _extract_holder_identity(accounts_data)
    _persist_accounts(connection, customer, accounts_data)
    return _mark_banking_success(connection, customer, flinks_email, flinks_phone, flinks_name)


def pending_ibv_repull_targets(*, since=None, customer_id=None, include_syncing=False):
    """Latest Flinks LoginId per customer that still needs IBV.

    Includes Arrive and landing-page customers — source is not filtered.
    Arrive is only the embed channel; GAD still uses the stored Flinks LoginId.

    Includes failed / pending (and inactive post-reapply) connections. Skips
    verified+synced and manual void-cheque rows. Currently ``syncing`` rows are
    skipped unless ``include_syncing`` so we do not double-queue a live pull.
    """
    from django.db.models import Exists, OuterRef, Q
    from loans.models import Loan

    pending_loan = Loan.objects.filter(
        customer_id=OuterRef('customer_id'),
        status='ibv_pending',
    )
    qs = (
        BankConnection.objects.select_related('customer')
        .filter(provider='flinks')
        .exclude(login_id='')
        .filter(Q(customer__banking_verified=False) | Exists(pending_loan))
        .exclude(customer__banking_verified=True, sync_status='synced')
    )
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    if since is not None:
        qs = qs.filter(updated_at__gte=since)
    if not include_syncing:
        qs = qs.exclude(sync_status='syncing')

    seen = set()
    targets = []
    for connection in qs.order_by('-created_at', '-id'):
        if not str(connection.login_id or '').strip():
            continue
        if connection.customer_id in seen:
            continue
        seen.add(connection.customer_id)
        targets.append(connection)
    return targets


def queue_flinks_gad_repull(
    connection,
    *,
    user=None,
    inline=False,
    trigger=None,
):
    """Re-run Authorize + GetAccountsDetail for an existing Flinks LoginId.

    Staff / portal keep ``delay()`` so the HTTP request can return. Automated
    re-pull (beat, ``--inline``) uses ``apply()`` so GAD actually runs — Redis
    countdown/delay from the worker often logs started without pulling IBV.
    """
    if connection.provider != 'flinks':
        raise ValueError('Only Flinks connections support GetAccountsDetail re-pull.')
    if not str(connection.login_id or '').strip():
        raise ValueError('Connection has no Flinks LoginId to re-pull.')

    customer = connection.customer
    if customer.banking_verified and connection.sync_status == 'synced':
        raise ValueError(
            'Banking is already verified; GetAccountsDetail re-pull is not needed.'
        )

    connection.is_active = True
    connection.sync_status = 'pending'
    connection.sync_error = None
    connection.save(
        update_fields=['is_active', 'sync_status', 'sync_error', 'updated_at']
    )

    actor = 'system'
    if user is not None:
        actor = str(getattr(user, 'id', None) or getattr(user, 'email', None) or 'staff')

    try:
        from activity.models import ActivityHistory

        description = (
            'GetAccountsDetail re-pull queued for the existing Flinks LoginId.'
        )
        if trigger:
            description = f'{description} ({str(trigger)[:180]})'

        ActivityHistory.objects.create(
            customer=customer,
            type='system',
            title='IBV Re-pull Started',
            description=description,
            created_by=actor,
            metadata={
                'action': 'flinks_gad_repull',
                'connection_id': str(connection.id),
                'login_id': str(connection.login_id),
                'automated': user is None,
                'inline': inline,
                'trigger': (str(trigger)[:200] if trigger else ''),
            },
        )
    except Exception:
        logger.exception(
            'Failed to log Flinks GAD re-pull activity customer=%s connection=%s',
            customer.id,
            connection.id,
        )

    if inline:
        fetch_flinks_accounts_only.apply(args=[str(connection.id)])
    else:
        fetch_flinks_accounts_only.delay(str(connection.id))

    logger.info(
        'Flinks GAD re-pull queued customer_id=%s connection_id=%s login_id=%s '
        'actor=%s inline=%s',
        customer.id,
        connection.id,
        mask_identifier(connection.login_id),
        actor,
        inline,
    )
    return connection


@shared_task
def repull_recent_unsynced_ibv():
    """Pending IBV with a LoginId: run the same GAD pull as the staff button.

    Executes inline so the pull cannot be lost after Redis accepts a delay().
    """
    queued = 0
    skipped = 0
    for connection in unsynced_ibv_safety_net_targets()[:FLINKS_GAD_SAFETY_NET_BATCH_SIZE]:
        try:
            queue_flinks_gad_repull(
                connection,
                trigger='pending_ibv',
                inline=True,
            )
        except ValueError:
            skipped += 1
            continue
        queued += 1
    logger.info(
        'Flinks GAD safety-net queued=%s skipped=%s',
        queued,
        skipped,
    )
    return {'queued': queued, 'skipped': skipped}


def _delete_unsupported_banking_connection(connection, customer, reason, institutions):
    logger.warning(
        'Flinks sync deleting unsupported institution connection customer_id=%s connection_id=%s login_id=%s reason=%s',
        customer.id,
        connection.id,
        mask_identifier(connection.login_id),
        reason,
    )

    customer.banking_verified = False
    if customer.onboarding_stage != 'banking_verification':
        customer.onboarding_stage = 'banking_verification'
    customer.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])

    _log_banking_failure(
        customer,
        'Banking Verification Reset',
        reason,
        metadata={
            'reason_code': UNSUPPORTED_IBV_REASON_CODE,
            'unsupported_institutions': sorted(institutions),
        },
    )
    send_banking_retry_email.delay(str(customer.id), reason)
    connection.delete()
    logger.warning('Unsupported Flinks connection deleted customer_id=%s', customer.id)
    return False


def _persist_accounts(connection, customer, accounts_data):
    primary_assigned = False
    risk_primary_candidate = None
    account_count = 0
    transaction_count = 0

    for acc in accounts_data:
        account_count += 1
        normalized_type = _normalize_account_type(acc.get('Type'))

        account_obj, _created = BankAccount.objects.update_or_create(
            connection=connection,
            external_id=acc.get('Id'),
            defaults={
                'customer': customer,
                'name': acc.get('Title') or acc.get('AccountNumber') or 'Unknown account',
                'type': normalized_type,
                'currency': acc.get('Currency') or 'CAD',
                'balance': acc.get('Balance', {}).get('Current')
                if isinstance(acc.get('Balance'), dict) else None,
                'transit_number': acc.get('TransitNumber'),
                'institution_number': acc.get('InstitutionNumber'),
                'account_number': acc.get('AccountNumber'),
                'is_primary': False,
            },
        )

        if not primary_assigned and normalized_type in {'checking', 'savings'}:
            # Prefer non-risk banks for primary; allow 621/623/703 when that is all
            # the customer has (agent warning before funding/collections).
            if is_payment_blocked_institution(acc.get('InstitutionNumber')):
                if risk_primary_candidate is None:
                    risk_primary_candidate = account_obj
            else:
                account_obj.is_primary = True
                account_obj.save(update_fields=['is_primary', 'updated_at'])
                primary_assigned = True

        transactions = acc.get('Transactions') or []
        transaction_count += len(transactions)
        for tx in transactions:
            BankTransaction.objects.update_or_create(
                account=account_obj,
                external_id=tx.get('Id'),
                defaults={
                    'customer': customer,
                    'date': tx.get('Date'),
                    'description': tx.get('Description') or '',
                    'debit': tx.get('Debit'),
                    'credit': tx.get('Credit'),
                    'balance': tx.get('Balance'),
                },
            )

    if not primary_assigned and risk_primary_candidate is not None:
        risk_primary_candidate.is_primary = True
        risk_primary_candidate.save(update_fields=['is_primary', 'updated_at'])
        primary_assigned = True

    logger.info(
        'Flinks accounts persisted customer_id=%s connection_id=%s accounts=%s transactions=%s primary_assigned=%s',
        customer.id,
        connection.id,
        account_count,
        transaction_count,
        primary_assigned,
    )


def _mark_banking_success(connection, customer, flinks_email=None, flinks_phone=None, flinks_name=None):
    with transaction.atomic():
        connection.last_synced_at = now()
        connection.sync_status = 'synced'
        connection.sync_error = None
        connection.is_active = True
        connection.save(update_fields=['last_synced_at', 'sync_status', 'sync_error', 'is_active', 'updated_at'])

        customer.banking_verified = True
        if customer.onboarding_stage == 'banking_verification':
            customer.onboarding_stage = 'contract'
        customer.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])

        from loans.services import LoanService
        for loan in customer.loans.filter(status='ibv_pending'):
            LoanService.mark_pending_signature(loan)

        portal_user = customer.portal_user
        if portal_user:
            update_fields = []
            if flinks_email:
                portal_user.flinks_email = flinks_email
                update_fields.append('flinks_email')
            if flinks_phone:
                portal_user.flinks_phone = flinks_phone
                update_fields.append('flinks_phone')
            if flinks_name:
                portal_user.flinks_name = flinks_name
                update_fields.append('flinks_name')
            if update_fields:
                update_fields.append('updated_at')
                portal_user.save(update_fields=update_fields)

    try:
        from activity.models import ActivityHistory

        ActivityHistory.objects.create(
            customer=customer,
            type='ibv_completed',
            title='Banking Verification Completed',
            description='Customer bank account data synced successfully.',
            created_by='system',
        )
    except Exception:
        logger.exception('Failed to log banking success activity for customer=%s', customer.id)

    logger.info(
        'Flinks sync succeeded customer_id=%s connection_id=%s login_id=%s',
        customer.id,
        connection.id,
        mask_identifier(connection.login_id),
    )
    return True


@shared_task
def send_banking_retry_email(customer_id, failure_reason=''):
    try:
        from django.core.mail import send_mail

        from accounts.models import Customer

        customer = Customer.objects.get(id=customer_id)
        frontend_url = settings.FRONTEND_URL.rstrip('/')
        banking_url = f'{frontend_url}/customer/banking'
        is_unsupported_institution = UNSUPPORTED_INSTITUTION_MESSAGE in (failure_reason or '')
        action_copy = (
            'The bank account you connected is not supported for IBV. '
            'Please refill IBV by reconnecting with a supported bank account. '
            'If you reconnect the same unsupported institution again, it will not count as completed.'
            if is_unsupported_institution
            else 'Please reconnect your bank account.'
        )

        send_mail(
            subject='Action Required: Please reconnect your bank account',
            message=(
                f'Hello {customer.first_name},\n\n'
                f'We were unable to complete your banking verification.\n\n'
                f'{failure_reason}\n\n'
                f'{action_copy}\n\n'
                f'Refill IBV here:\n{banking_url}\n\n'
                f'Thank you,\n{settings.LENDER_BRAND_NAME}'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[customer.email],
            fail_silently=False,
        )
        logger.info('Banking retry email sent to customer=%s', customer.email)
    except Exception as exc:
        logger.error('Error sending banking retry email: %s', exc)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 30})
def fetch_flinks_accounts_only(self, connection_id):
    """
    Fetch accounts and transactions from Flinks and sync them to DB.
    """
    connection = BankConnection.objects.select_related('customer').filter(id=connection_id).first()
    if not connection:
        logger.error('No BankConnection found for connection_id=%s', connection_id)
        return False

    customer = connection.customer
    login_id = connection.login_id
    logger.info(
        'Flinks sync started customer_id=%s connection_id=%s login_id=%s task_id=%s',
        customer.id,
        connection.id,
        mask_identifier(login_id),
        getattr(self.request, 'id', None),
    )
    BankConnection.objects.filter(id=connection.id).update(
        sync_status='syncing',
        sync_error=None,
        attempted_syncs=F('attempted_syncs') + 1,
        updated_at=now(),
    )
    connection.refresh_from_db()

    from accounts.models import GlobalSetting

    customer_id = GlobalSetting.get_value('FLINKS_CUSTOMER_ID', settings.FLINKS_CUSTOMER_ID)
    secret_key = GlobalSetting.get_value('FLINKS_SECRET_KEY_CA', settings.FLINKS_SECRET_KEY_CA)
    instance = GlobalSetting.get_value('FLINKS_INSTANCE', settings.FLINKS_INSTANCE)
    logger.info(
        'Flinks sync config customer_id=%s connection_id=%s instance=%s customer_id_configured=%s secret_key_configured=%s',
        customer.id,
        connection.id,
        instance,
        bool(customer_id),
        bool(secret_key),
    )

    headers = _flinks_headers(secret_key)

    auth_url = f'https://{instance}-api.private.fin.ag/v3/{customer_id}/BankingServices/Authorize'

    def _authorize():
        logger.info(
            'Flinks Authorize request customer_id=%s connection_id=%s login_id=%s most_recent_cached=%s',
            customer.id,
            connection.id,
            mask_identifier(login_id),
            FLINKS_MOST_RECENT_CACHED,
        )
        return requests.post(
            auth_url,
            json={
                'LoginId': str(login_id),
                'MostRecentCached': FLINKS_MOST_RECENT_CACHED,
            },
            headers=headers,
            timeout=FLINKS_HTTP_TIMEOUT,
        )

    def _get_accounts_detail(request_id):
        logger.info(
            'Flinks GetAccountsDetail request customer_id=%s connection_id=%s request_id=%s',
            customer.id,
            connection.id,
            mask_identifier(request_id),
        )
        acct_resp = requests.post(
            acct_url,
            json={'RequestId': request_id, 'DaysOfTransactions': 'Days365'},
            headers=headers,
            timeout=FLINKS_HTTP_TIMEOUT,
        )
        logger.info(
            'Flinks GetAccountsDetail response customer_id=%s connection_id=%s request_id=%s status=%s',
            customer.id,
            connection.id,
            mask_identifier(request_id),
            acct_resp.status_code,
        )
        if acct_resp.status_code == 200:
            return acct_resp.json()
        if acct_resp.status_code == 202:
            logger.info(
                'Flinks GetAccountsDetail async accepted customer_id=%s connection_id=%s request_id=%s',
                customer.id,
                connection.id,
                mask_identifier(request_id),
            )
            polled = _poll_get_accounts_detail_async(
                instance, customer_id, request_id, headers
            )
            return polled if polled is not None else 'TRANSIENT'
        if _is_flinks_security_challenge(acct_resp):
            return 'SECURITY_CHALLENGE'
        if _is_transient_flinks_api_error(acct_resp):
            logger.warning(
                'Flinks GetAccountsDetail transient customer_id=%s connection_id=%s '
                'status=%s code=%s',
                customer.id,
                connection.id,
                acct_resp.status_code,
                _flinks_code_from_response(acct_resp),
            )
            return 'TRANSIENT'
        logger.error(
            'Flinks GetAccountsDetail failed customer_id=%s connection_id=%s status=%s body=%s',
            customer.id,
            connection.id,
            acct_resp.status_code,
            acct_resp.text,
        )
        return 'FAILED'

    try:
        auth_resp = _authorize()
    except requests.RequestException as exc:
        logger.exception(
            'Flinks Authorize request error customer_id=%s connection_id=%s login_id=%s',
            customer.id,
            connection.id,
            mask_identifier(login_id),
        )
        return _handle_flinks_transport_error(
            self,
            connection,
            customer,
            exc,
            instance=instance,
            flinks_customer_id=customer_id,
            headers=headers,
            login_id=login_id,
        )

    logger.info(
        'Flinks Authorize response customer_id=%s connection_id=%s status=%s',
        customer.id,
        connection.id,
        auth_resp.status_code,
    )
    if _is_flinks_security_challenge(auth_resp):
        return _recover_ibv_without_live_pull(
            self,
            connection,
            customer,
            auth_resp.text,
            instance=instance,
            flinks_customer_id=customer_id,
            headers=headers,
            login_id=login_id,
        )
    if auth_resp.status_code != 200:
        if _is_transient_flinks_api_error(auth_resp):
            return _recover_ibv_without_live_pull(
                self,
                connection,
                customer,
                auth_resp.text,
                instance=instance,
                flinks_customer_id=customer_id,
                headers=headers,
                login_id=login_id,
            )
        return _mark_banking_failed(connection, customer, auth_resp.text)

    request_id = auth_resp.json().get('RequestId')
    if not request_id:
        return _mark_banking_failed(connection, customer, 'No RequestId returned by Flinks')
    logger.info(
        'Flinks Authorize accepted customer_id=%s connection_id=%s request_id=%s',
        customer.id,
        connection.id,
        mask_identifier(request_id),
    )

    acct_url = f'https://{instance}-api.private.fin.ag/v3/{customer_id}/BankingServices/GetAccountsDetail'

    try:
        accounts_json = _get_accounts_detail(request_id)
    except requests.RequestException as exc:
        logger.exception(
            'Flinks GetAccountsDetail request error customer_id=%s connection_id=%s request_id=%s',
            customer.id,
            connection.id,
            mask_identifier(request_id),
        )
        return _handle_flinks_transport_error(
            self,
            connection,
            customer,
            exc,
            instance=instance,
            flinks_customer_id=customer_id,
            headers=headers,
            login_id=login_id,
        )

    if accounts_json == 'SECURITY_CHALLENGE':
        return _recover_ibv_without_live_pull(
            self,
            connection,
            customer,
            'Flinks GetAccountsDetail returned a security challenge.',
            instance=instance,
            flinks_customer_id=customer_id,
            headers=headers,
            login_id=login_id,
        )
    if accounts_json == 'TRANSIENT':
        return _recover_ibv_without_live_pull(
            self,
            connection,
            customer,
            'OPERATION_PENDING: GetAccountsDetail still processing for a large IBV.',
            instance=instance,
            flinks_customer_id=customer_id,
            headers=headers,
            login_id=login_id,
        )
    if accounts_json == 'FAILED' or not accounts_json:
        if connection.attempted_syncs < FLINKS_GAD_AUTOMATED_MAX_ATTEMPTS:
            _mark_awaiting_flinks_webhook(
                connection,
                'Failed to fetch accounts from Flinks',
            )
            return False
        return _mark_banking_failed(connection, customer, 'Failed to fetch accounts from Flinks')

    accounts_data = accounts_json.get('Accounts') or []
    if not accounts_data:
        if connection.attempted_syncs < FLINKS_GAD_AUTOMATED_MAX_ATTEMPTS:
            _mark_awaiting_flinks_webhook(connection, NO_ACCOUNTS_MESSAGE)
            return False
        return _mark_banking_failed(connection, customer, NO_ACCOUNTS_MESSAGE)

    total_transactions = _count_transactions(accounts_data)
    logger.info(
        'Flinks payload received customer_id=%s connection_id=%s accounts=%s transactions=%s',
        customer.id,
        connection.id,
        len(accounts_data),
        total_transactions,
    )

    # Cached/partial pulls can return accounts before transactions land (common for
    # KOHO / Arrive). Retry GetAccountsDetail before failing IBV.
    for retry in range(1, FLINKS_ZERO_TX_RETRIES + 1):
        if total_transactions > 0:
            break
        logger.info(
            'Flinks zero-transaction retry=%s/%s customer_id=%s connection_id=%s',
            retry,
            FLINKS_ZERO_TX_RETRIES,
            customer.id,
            connection.id,
        )
        time.sleep(FLINKS_ZERO_TX_RETRY_SECONDS)
        try:
            # Re-authorize for a fresh RequestId, then pull detail again.
            auth_resp = _authorize()
            if auth_resp.status_code != 200:
                continue
            request_id = auth_resp.json().get('RequestId') or request_id
            accounts_json = _get_accounts_detail(request_id)
        except requests.RequestException:
            logger.exception(
                'Flinks zero-transaction retry failed customer_id=%s connection_id=%s',
                customer.id,
                connection.id,
            )
            continue
        if not isinstance(accounts_json, dict):
            continue
        accounts_data = accounts_json.get('Accounts') or accounts_data
        total_transactions = _count_transactions(accounts_data)
        logger.info(
            'Flinks zero-transaction retry result customer_id=%s connection_id=%s transactions=%s',
            customer.id,
            connection.id,
            total_transactions,
        )

    # 621/623/703 are persisted like any other bank. Agents may decline with
    # "Unsupported bank"; funding UI shows a non-blocking risk warning.
    if total_transactions == 0 and connection.attempted_syncs < FLINKS_GAD_AUTOMATED_MAX_ATTEMPTS:
        _mark_awaiting_flinks_webhook(
            connection,
            'NO_TRANSACTION: Flinks returned accounts before history was ready.',
        )
        return False
    return apply_flinks_accounts_detail(connection, {'Accounts': accounts_data})
