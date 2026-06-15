import logging
import time
import requests

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from .models import BankConnection, BankAccount, BankTransaction

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={"max_retries": 3, "countdown": 30})
def fetch_flinks_accounts_only(self, login_id):
    """
    Fetch accounts and transactions from Flinks and sync them to DB.
    """
    connection = BankConnection.objects.select_related('customer').filter(login_id=login_id).first()
    if not connection:
        logger.error(f"No BankConnection found for login_id={login_id}")
        return False

    customer = connection.customer
    connection.sync_status = 'syncing'
    connection.sync_error = None
    connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

    customer_id = settings.FLINKS_CUSTOMER_ID
    secret_key = settings.FLINKS_SECRET_KEY_CA
    instance = settings.FLINKS_INSTANCE

    headers = {
        "Content-Type": "application/json",
        "flinks-auth-key": secret_key,
    }

    auth_url = f"https://{instance}-api.private.fin.ag/v3/{customer_id}/BankingServices/Authorize"

    try:
        auth_resp = requests.post(
            auth_url,
            json={"LoginId": str(login_id), "MostRecentCached": True},
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as e:
        connection.sync_status = 'failed'
        connection.sync_error = str(e)
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        logger.error(f"Flinks authorization network error: {e}")
        raise

    if auth_resp.status_code != 200:
        connection.sync_status = 'failed'
        connection.sync_error = auth_resp.text
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        logger.error(f"Flinks Authorize failed: {auth_resp.text}")
        return False

    request_id = auth_resp.json().get("RequestId")
    if not request_id:
        connection.sync_status = 'failed'
        connection.sync_error = "No RequestId returned by Flinks"
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        return False

    acct_url = f"https://{instance}-api.private.fin.ag/v3/{customer_id}/BankingServices/GetAccountsDetail"

    try:
        acct_resp = requests.post(
            acct_url,
            json={"RequestId": request_id, "DaysOfTransactions": "Days365"},
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as e:
        connection.sync_status = 'failed'
        connection.sync_error = str(e)
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        logger.error(f"Flinks GetAccounts network error: {e}")
        raise

    accounts_json = None

    if acct_resp.status_code == 200:
        accounts_json = acct_resp.json()
    elif acct_resp.status_code == 202:
        async_url = f"https://{instance}-api.private.fin.ag/v3/{customer_id}/BankingServices/GetAccountsDetailAsync/{request_id}"
        for _ in range(20):
            time.sleep(10)
            async_resp = requests.get(async_url, headers=headers, timeout=30)
            if async_resp.status_code == 200:
                accounts_json = async_resp.json()
                break
    else:
        connection.sync_status = 'failed'
        connection.sync_error = acct_resp.text
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        logger.error(f"Flinks GetAccountsDetail failed. Status: {acct_resp.status_code}, Response: {acct_resp.text}")

    if not accounts_json:
        connection.sync_status = 'failed'
        connection.sync_error = "Failed to fetch accounts from Flinks"
        connection.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
        logger.error(f"Failed to fetch accounts for login_id={login_id}")
        return False

    accounts_data = accounts_json.get("Accounts", [])

    primary_assigned = False

    for acc in accounts_data:
        raw_type = (acc.get("Type") or "other").lower()
        normalized_type = raw_type if raw_type in {'checking', 'savings', 'credit', 'loan', 'investment'} else 'other'

        account_obj, created = BankAccount.objects.update_or_create(
            customer=customer,
            external_id=acc.get("Id"),
            defaults={
                "connection": connection,
                "name": acc.get("Title") or acc.get("AccountNumber") or "Unknown account",
                "type": normalized_type,
                "currency": acc.get("Currency") or "CAD",
                "balance": acc.get("Balance", {}).get("Current") if isinstance(acc.get("Balance"), dict) else None,
                "transit_number": acc.get("TransitNumber"),
                "institution_number": acc.get("InstitutionNumber"),
                "account_number": acc.get("AccountNumber"),
                "is_primary": False,
            },
        )

        if not primary_assigned and normalized_type in {'checking', 'savings'}:
            account_obj.is_primary = True
            account_obj.save(update_fields=['is_primary', 'updated_at'])
            primary_assigned = True

        transactions = acc.get("Transactions", [])
        for tx in transactions:
            BankTransaction.objects.update_or_create(
                account=account_obj,
                external_id=tx.get("Id"),
                defaults={
                    "customer": customer,
                    "date": tx.get("Date"),
                    "description": tx.get("Description") or "",
                    "debit": tx.get("Debit"),
                    "credit": tx.get("Credit"),
                    "balance": tx.get("Balance"),
                },
            )

    connection.last_synced_at = now()
    connection.sync_status = 'synced'
    connection.sync_error = None
    connection.save(update_fields=['last_synced_at', 'sync_status', 'sync_error', 'updated_at'])

    customer.banking_verified = True
    if customer.onboarding_stage == 'banking_verification':
        customer.onboarding_stage = 'references'
    customer.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])

    logger.info(f"Flinks synced successfully for customer={customer.email}")
    return True