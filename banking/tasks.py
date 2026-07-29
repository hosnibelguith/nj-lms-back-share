import logging
import time

import requests
from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from .logging_utils import mask_identifier
from .models import BankConnection, BankAccount, BankTransaction

logger = logging.getLogger(__name__)

FLINKS_ASYNC_POLL_INTERVAL_SECONDS = 10
FLINKS_ASYNC_MAX_WAIT_SECONDS = 30 * 60
ZERO_TRANSACTIONS_MESSAGE = (
    'We could not retrieve transaction history from your bank account. '
    'Please reconnect your bank account.'
)
NO_ACCOUNTS_MESSAGE = (
    'No bank accounts were returned. Please reconnect your bank account.'
)


def _normalize_account_type(raw_type):
    value = (raw_type or 'other').strip().lower()
    if value in {'chequing', 'checking'}:
        return 'checking'
    if value in {'savings', 'saving'}:
        return 'savings'
    if value in {'credit', 'loan', 'investment'}:
        return value
    return 'other'


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
        async_resp = requests.get(async_url, headers=headers, timeout=30)
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


def _log_banking_failure(customer, title, description):
    try:
        from activity.models import ActivityHistory

        ActivityHistory.objects.create(
            customer=customer,
            type='system',
            title=title,
            description=description,
            created_by='system',
            metadata={'source': 'flinks_sync'},
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


def _persist_accounts(connection, customer, accounts_data):
    primary_assigned = False
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
    logger.info(
        'Flinks accounts persisted customer_id=%s connection_id=%s accounts=%s transactions=%s primary_assigned=%s',
        customer.id,
        connection.id,
        account_count,
        transaction_count,
        primary_assigned,
    )


def _mark_banking_success(connection, customer, flinks_email=None, flinks_phone=None, flinks_name=None):
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

        send_mail(
            subject='Action Required: Please reconnect your bank account',
            message=(
                f'Hello {customer.first_name},\n\n'
                f'We were unable to complete your banking verification.\n\n'
                f'{failure_reason}\n\n'
                f'Please reconnect your bank account here:\n{banking_url}\n\n'
                f'Thank you,\nLendStack'
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
    connection.sync_status = 'syncing'
    connection.sync_error = None
    connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

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

    try:
        logger.info(
            'Flinks Authorize request customer_id=%s connection_id=%s login_id=%s',
            customer.id,
            connection.id,
            mask_identifier(login_id),
        )
        auth_resp = requests.post(
            auth_url,
            json={'LoginId': str(login_id), 'MostRecentCached': True},
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.exception(
            'Flinks Authorize request error customer_id=%s connection_id=%s login_id=%s',
            customer.id,
            connection.id,
            mask_identifier(login_id),
        )
        return _mark_banking_failed(connection, customer, str(exc))

    logger.info(
        'Flinks Authorize response customer_id=%s connection_id=%s status=%s',
        customer.id,
        connection.id,
        auth_resp.status_code,
    )
    if auth_resp.status_code != 200:
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
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.exception(
            'Flinks GetAccountsDetail request error customer_id=%s connection_id=%s request_id=%s',
            customer.id,
            connection.id,
            mask_identifier(request_id),
        )
        return _mark_banking_failed(connection, customer, str(exc))

    logger.info(
        'Flinks GetAccountsDetail response customer_id=%s connection_id=%s request_id=%s status=%s',
        customer.id,
        connection.id,
        mask_identifier(request_id),
        acct_resp.status_code,
    )
    if acct_resp.status_code == 200:
        accounts_json = acct_resp.json()
    elif acct_resp.status_code == 202:
        logger.info(
            'Flinks GetAccountsDetail async accepted customer_id=%s connection_id=%s request_id=%s',
            customer.id,
            connection.id,
            mask_identifier(request_id),
        )
        accounts_json = _poll_get_accounts_detail_async(instance, customer_id, request_id, headers)
    else:
        return _mark_banking_failed(
            connection,
            customer,
            f'Flinks GetAccountsDetail failed ({acct_resp.status_code})',
        )

    if not accounts_json:
        return _mark_banking_failed(connection, customer, 'Failed to fetch accounts from Flinks')

    accounts_data = accounts_json.get('Accounts') or []

    if not accounts_data:
        return _mark_banking_failed(connection, customer, NO_ACCOUNTS_MESSAGE)

    total_transactions = _count_transactions(accounts_data)
    logger.info(
        'Flinks payload received customer_id=%s connection_id=%s accounts=%s transactions=%s',
        customer.id,
        connection.id,
        len(accounts_data),
        total_transactions,
    )
    if total_transactions == 0:
        return _mark_banking_failed(connection, customer, ZERO_TRANSACTIONS_MESSAGE)

    flinks_email, flinks_phone, flinks_name = _extract_holder_identity(accounts_data)
    _persist_accounts(connection, customer, accounts_data)
    return _mark_banking_success(connection, customer, flinks_email, flinks_phone, flinks_name)
