import logging
import secrets
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

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
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == 'N/A':
        return None
    return text


def _parse_optional_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return parse_datetime(str(value))


def _coords_complete(primary: dict) -> bool:
    return bool(
        _normalize_coord(primary.get('institution_number'))
        and _normalize_coord(primary.get('transit_number'))
        and _normalize_coord(primary.get('account_number'))
    )


def _find_matching_account(connection: BankConnection, primary: dict):
    institution = _normalize_coord(primary.get('institution_number'))
    transit = _normalize_coord(primary.get('transit_number'))
    account_number = _normalize_coord(primary.get('account_number'))
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
    institution = _normalize_coord(primary.get('institution_number'))
    transit = _normalize_coord(primary.get('transit_number'))
    account_number = _normalize_coord(primary.get('account_number'))

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
        status__in=['paid_off', 'defaulted', 'ai_declined', 'human_declined']
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

    return event, False


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
