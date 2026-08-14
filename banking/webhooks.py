import logging
import secrets
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .constants import (
    bank_coordinates_complete,
    is_payment_blocked_institution,
    normalize_bank_coordinate,
    normalize_institution_number,
)
from .models import (
    BankAccount,
    BankConnection,
    BankingAnalysisEvent,
    FinancialAnalysisReport,
)
from .logging_utils import mask_identifier

logger = logging.getLogger(__name__)


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'token':
        return None
    return parts[1].strip() or None


def _api_key_valid(provided: str | None) -> bool:
    expected = getattr(settings, 'MOHAWK_BANKING_ANALYSIS_API_KEY', '') or ''
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def _normalize_coord(value):
    return normalize_bank_coordinate(value)


def _parse_optional_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return parse_datetime(str(value))


def _coords_complete(primary: dict) -> bool:
    return bank_coordinates_complete(primary)


def _find_matching_account(connection: BankConnection, primary: dict):
    institution = normalize_bank_coordinate(primary.get('institution_number'))
    transit = normalize_bank_coordinate(primary.get('transit_number'))
    account_number = normalize_bank_coordinate(primary.get('account_number'))
    if not (institution and transit and account_number):
        return None

    return connection.accounts.filter(
        institution_number=institution,
        transit_number=transit,
        account_number=account_number,
    ).first()


def _apply_primary_eft_account(connection: BankConnection, primary: dict):
    """
    Mark primary_bank_account as the operational EFT funding + collections account
    for this bank connection (and unlockable loan FKs for the customer).
    """
    institution = normalize_bank_coordinate(primary.get('institution_number'))
    transit = normalize_bank_coordinate(primary.get('transit_number'))
    account_number = normalize_bank_coordinate(primary.get('account_number'))

    account = _find_matching_account(connection, primary)
    if account is None:
        external_id = f"mohawk-primary-{institution}-{transit}-{account_number}"
        account, _created = BankAccount.objects.update_or_create(
            connection=connection,
            external_id=external_id,
            defaults={
                'customer': connection.customer,
                'name': f'Primary EFT {institution}-{transit}',
                'type': 'checking',
                'currency': 'CAD',
                'institution_number': institution,
                'transit_number': transit,
                'account_number': account_number,
            },
        )
    else:
        account.institution_number = institution
        account.transit_number = transit
        account.account_number = account_number

    connection.accounts.exclude(pk=account.pk).update(
        is_primary=False,
        use_for_eft_funding=False,
        use_for_eft_collections=False,
    )
    account.is_primary = True
    account.use_for_eft_funding = True
    account.use_for_eft_collections = True
    account.save(
        update_fields=[
            'institution_number',
            'transit_number',
            'account_number',
            'is_primary',
            'use_for_eft_funding',
            'use_for_eft_collections',
            'updated_at',
        ]
    )

    from loans.models import Loan
    from loans.zumrails import account_snapshot

    loans = Loan.objects.filter(customer=connection.customer).exclude(
        status__in=['paid_off', 'defaulted', 'human_declined']
    )
    for loan in loans:
        update_fields = []
        if not loan.funding_destination_locked_at:
            loan.bank_account = account
            update_fields.append('bank_account')
            destination = dict(loan.funding_destination or {})
            destination['eft'] = {
                'bank_account_id': str(account.id),
                'account': account_snapshot(account),
            }
            loan.funding_destination = destination
            update_fields.append('funding_destination')
        if not loan.collections_account_locked_at:
            loan.collections_account = account
            update_fields.append('collections_account')
        if update_fields:
            update_fields.append('updated_at')
            loan.save(update_fields=update_fields)

    return account


def _complete_ibv_from_mohawk_analysis(connection, *, primary: dict, source_transactions) -> bool:
    """Advance stuck IBV when Mohawk analysis proves banking data exists.

    Flinks toolbox / Mohawk can finish while LMS Celery sync still failed
    (stale MostRecentCached / 0-tx). Analysis webhook is a second signal to
    leave ``ibv_pending`` so staff are not blocked after a successful IBV.
    """
    if connection is None or connection.customer_id is None:
        return False

    has_coords = _coords_complete(primary if isinstance(primary, dict) else {})
    has_txs = isinstance(source_transactions, list) and len(source_transactions) > 0
    has_accounts = connection.accounts.exists()
    if not (has_coords or has_txs or has_accounts):
        return False

    customer = connection.customer
    pending_loans = list(customer.loans.filter(status='ibv_pending'))
    if customer.banking_verified and not pending_loans:
        return False

    from loans.services import LoanService

    with transaction.atomic():
        connection.last_synced_at = timezone.now()
        connection.sync_status = 'synced'
        connection.sync_error = None
        connection.is_active = True
        connection.save(
            update_fields=[
                'last_synced_at',
                'sync_status',
                'sync_error',
                'is_active',
                'updated_at',
            ]
        )

        customer.banking_verified = True
        if customer.onboarding_stage == 'banking_verification':
            customer.onboarding_stage = 'contract'
        customer.save(update_fields=['banking_verified', 'onboarding_stage', 'updated_at'])

        for loan in pending_loans:
            LoanService.mark_pending_signature(loan)

    try:
        from activity.models import ActivityHistory

        ActivityHistory.objects.create(
            customer=customer,
            type='ibv_completed',
            title='Banking Verification Completed',
            description=(
                'Banking verification completed from Mohawk analysis '
                '(Flinks data available).'
            ),
            created_by='system',
            metadata={'source': 'mohawk_banking_analysis'},
        )
    except Exception:
        logger.exception(
            'Failed to log Mohawk IBV completion activity customer=%s',
            customer.id,
        )

    logger.info(
        'IBV completed via Mohawk analysis customer_id=%s connection_id=%s login_id=%s',
        customer.id,
        connection.id,
        mask_identifier(connection.login_id),
    )
    return True


@transaction.atomic
def process_banking_analysis_payload(payload: dict):
    event_id = payload.get('event_id')
    if not event_id:
        raise ValueError('event_id is required')

    existing = BankingAnalysisEvent.objects.filter(event_id=event_id).first()
    if existing:
        logger.info(
            'Banking analysis webhook duplicate event_id=%s login_id=%s tag=%s',
            event_id,
            mask_identifier(existing.login_id),
            existing.tag,
        )
        return existing, True

    login_id = payload.get('login_id')
    if login_id is None:
        login_id = ''
    else:
        login_id = str(login_id)

    primary = payload.get('primary_bank_account') or {}
    if not isinstance(primary, dict):
        primary = {}

    connection = None
    if login_id:
        connection = (
            BankConnection.objects.select_related('customer')
            .filter(login_id=login_id)
            .order_by('-is_active', '-created_at')
            .first()
        )
    logger.info(
        'Banking analysis webhook processing event_id=%s login_id=%s tag=%s matched_connection=%s',
        event_id,
        mask_identifier(login_id),
        payload.get('tag') or '',
        bool(connection),
    )

    primary_risk = is_payment_blocked_institution(primary.get('institution_number'))
    eft_incomplete = not _coords_complete(primary)
    exception_note = ''
    primary_account = None

    if not login_id:
        exception_note = 'Missing login_id'
    elif connection is None:
        exception_note = 'No bank connection found for login_id'
    elif eft_incomplete:
        exception_note = 'Primary bank account coordinates incomplete'
    else:
        primary_account = _apply_primary_eft_account(connection, primary)
        if primary_risk:
            # Agent-review institutions (621/623/703) may still be used after verification.
            exception_note = (
                'Problematic bank (institution '
                f"{normalize_institution_number(primary.get('institution_number'))}) — "
                'verify other lenders were able to collect from this account '
                'before funding or collections.'
            )

    processing_status = 'exception' if exception_note and (
        not login_id or connection is None
    ) else 'accepted'
    if eft_incomplete and connection is not None:
        processing_status = 'accepted'

    event = BankingAnalysisEvent.objects.create(
        event_id=event_id,
        event=payload.get('event') or '',
        schema_version=payload.get('schema_version') or '',
        report_id=payload.get('report_id'),
        login_id=login_id,
        tag=payload.get('tag') or '',
        connection=connection,
        customer=connection.customer if connection else None,
        primary_account=primary_account,
        decision_1=payload.get('decision_1') or {},
        decision_2=payload.get('decision_2') or {},
        primary_bank_account=primary,
        report=payload.get('report') or {},
        final_report_text=payload.get('final_report_text') or '',
        source_transactions=payload.get('source_transactions') or [],
        raw_payload=payload,
        analysis_created_at=_parse_optional_datetime(payload.get('analysis_created_at')),
        generated_at=_parse_optional_datetime(payload.get('generated_at')),
        processing_status=processing_status,
        eft_setup_incomplete=eft_incomplete,
        exception_note=exception_note,
    )
    logger.info(
        'Banking analysis webhook stored event_id=%s status=%s exception=%s customer_id=%s connection_id=%s',
        event.event_id,
        event.processing_status,
        event.exception_note or '',
        event.customer_id,
        event.connection_id,
    )

    if connection and connection.customer:
        FinancialAnalysisReport.objects.create(
            customer=connection.customer,
            report_data={
                'event_id': event_id,
                'login_id': login_id,
                'decision_1': event.decision_1,
                'decision_2': event.decision_2,
                'primary_bank_account': primary,
                'report': event.report,
                'final_report_text': event.final_report_text,
                'source_transactions': event.source_transactions,
            },
        )
        logger.info(
            'Banking analysis report created event_id=%s customer_id=%s connection_id=%s',
            event.event_id,
            connection.customer_id,
            connection.id,
        )
        _complete_ibv_from_mohawk_analysis(
            connection,
            primary=primary,
            source_transactions=event.source_transactions,
        )

    return event, False


def process_flinks_webhook_payload(payload: dict) -> dict:
    """Ingest Flinks GetAccountsDetail webhook (same shape as a successful API pull).

    Flinks delivers account + transaction data here when processing completes. We
    must apply it even if our Authorize/GetAccountsDetail pull timed out.
    """
    if not isinstance(payload, dict):
        return {'status': 'ignored', 'reason': 'invalid_payload'}

    response_type = payload.get('ResponseType') or payload.get('responseType') or ''
    accounts = payload.get('Accounts')
    has_accounts = isinstance(accounts, list)

    if response_type and response_type != 'GetAccountsDetail':
        logger.info(
            'Flinks webhook ignored response_type=%s keys=%s',
            response_type,
            sorted(payload.keys()),
        )
        return {
            'status': 'ignored',
            'reason': 'unsupported_response_type',
            'response_type': response_type,
        }

    if not has_accounts:
        logger.info('Flinks webhook ignored reason=no_accounts keys=%s', sorted(payload.keys()))
        return {'status': 'ignored', 'reason': 'no_accounts'}

    login = payload.get('Login') if isinstance(payload.get('Login'), dict) else {}
    login_id = login.get('Id') or payload.get('LoginId') or payload.get('login_id')
    if not login_id:
        logger.warning('Flinks webhook ignored reason=missing_login_id')
        return {'status': 'ignored', 'reason': 'missing_login_id'}

    connection = (
        BankConnection.objects.select_related('customer')
        .filter(login_id=str(login_id))
        .order_by('-is_active', '-created_at')
        .first()
    )
    if connection is None:
        logger.warning(
            'Flinks webhook connection not found login_id=%s',
            mask_identifier(login_id),
        )
        return {'status': 'ignored', 'reason': 'connection_not_found'}

    from banking.tasks import apply_flinks_accounts_detail

    ok = apply_flinks_accounts_detail(connection, payload)
    logger.info(
        'Flinks webhook applied login_id=%s connection_id=%s customer_id=%s ok=%s',
        mask_identifier(login_id),
        connection.id,
        connection.customer_id,
        ok,
    )
    return {
        'status': 'synced' if ok else 'rejected',
        'connection_id': str(connection.id),
        'customer_id': str(connection.customer_id),
    }


class MohawkBankingAnalysisWebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        token = _extract_bearer_token(request.headers.get('Authorization'))
        if not _api_key_valid(token):
            logger.warning('Banking analysis webhook rejected invalid_api_key')
            return Response({'error': 'invalid_api_key'}, status=status.HTTP_401_UNAUTHORIZED)

        payload = request.data
        if not isinstance(payload, dict):
            logger.warning('Banking analysis webhook rejected invalid_json')
            return Response({'error': 'invalid_json'}, status=status.HTTP_400_BAD_REQUEST)

        event_id = payload.get('event_id')
        if not event_id:
            logger.warning('Banking analysis webhook rejected missing_event_id')
            return Response({'error': 'event_id_required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event, duplicate = process_banking_analysis_payload(payload)
        except ValueError as exc:
            logger.warning('Banking analysis webhook rejected event_id=%s error=%s', event_id, exc)
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        body = {
            'status': 'accepted',
            'event_id': event.event_id,
            'duplicate': duplicate,
        }
        logger.info(
            'Banking analysis webhook response event_id=%s duplicate=%s status_code=%s',
            event.event_id,
            duplicate,
            status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED,
        )
        if duplicate:
            return Response(body, status=status.HTTP_200_OK)
        return Response(body, status=status.HTTP_201_CREATED)
