import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from accounts.models import Customer
from activity.models import ActivityHistory

from .logging_utils import mask_identifier
from .models import BankConnection, BankAccount, BankTransaction, FinancialAnalysisReport
from .serializers import (
    BankConnectionSerializer,
    BankAccountSerializer,
    BankAccountManualCoordinatesSerializer,
    ManualBankAccountCreateSerializer,
    BankTransactionSerializer,
    FinancialAnalysisReportSerializer,
    CustomerPortalBankingStatusSerializer,
)
from .tasks import fetch_flinks_accounts_only, UNSUPPORTED_IBV_REASON_CODE

logger = logging.getLogger(__name__)


def _refresh_unlocked_loan_snapshots(account: BankAccount) -> None:
    """Keep pre-funding destination snapshots in sync after manual void-cheque edits."""
    from loans.models import Loan
    from loans.zumrails import account_snapshot

    loans = Loan.objects.filter(
        Q(bank_account=account) | Q(collections_account=account),
        funding_destination_locked_at__isnull=True,
    )
    for loan in loans:
        destination = loan.funding_destination if isinstance(loan.funding_destination, dict) else {}
        destination = dict(destination)
        eft = destination.get('eft') if isinstance(destination.get('eft'), dict) else {}
        eft = dict(eft)
        if (
            str(eft.get('bank_account_id') or '') == str(account.id)
            or loan.bank_account_id == account.id
        ):
            eft['bank_account_id'] = str(account.id)
            eft['account'] = account_snapshot(account)
            destination['eft'] = eft
            loan.funding_destination = destination
            loan.save(update_fields=['funding_destination', 'updated_at'])


def _log_bank_coords_activity(customer, *, title, description, user, metadata=None):
    ActivityHistory.objects.create(
        customer=customer,
        type='system',
        title=title,
        description=description,
        metadata=metadata or {},
        created_by=str(getattr(user, 'id', 'staff')),
    )


def _get_or_create_manual_connection(customer: Customer) -> BankConnection:
    connection = (
        BankConnection.objects.filter(customer=customer, provider='manual', is_active=True)
        .order_by('-created_at')
        .first()
    )
    if connection:
        return connection
    return BankConnection.objects.create(
        customer=customer,
        login_id=f'manual-{customer.id}',
        provider='manual',
        is_active=True,
        sync_status='synced',
        last_synced_at=timezone.now(),
    )


class StaffOnlyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, 'user_type', None) == 'staff'
        )


class CustomerPortalPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, 'user_type', None) == 'customer'
        )


class CustomerPortalBaseView(APIView):
    permission_classes = [CustomerPortalPermission]

    def get_customer(self, request):
        return request.user.customer_profile


class ConnectBankView(CustomerPortalBaseView):
    def permission_denied(self, request, message=None, code=None):
        logger.warning(
            'Flinks connect rejected: permission denied user_id=%s is_authenticated=%s user_type=%s',
            getattr(request.user, 'id', None),
            bool(request.user and request.user.is_authenticated),
            getattr(request.user, 'user_type', None),
        )
        return super().permission_denied(request, message=message, code=code)

    def post(self, request):
        login_id = request.data.get('login_id')

        if not login_id:
            logger.warning(
                'Flinks connect rejected: missing login_id user_id=%s',
                getattr(request.user, 'id', None),
            )
            return Response({"error": "Login ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        customer = self.get_customer(request)
        logger.info(
            'Flinks connect received customer_id=%s login_id=%s',
            customer.id,
            mask_identifier(login_id),
        )
        
        BankConnection.objects.filter(customer=customer, is_active=True).update(is_active=False)

        connection = BankConnection.objects.create(
            customer=customer,
            login_id=login_id,
            provider='flinks',
            is_active=True,
            sync_status='pending',
        )

        fetch_flinks_accounts_only.delay(str(connection.id))
        logger.info(
            'Flinks sync task queued customer_id=%s connection_id=%s login_id=%s',
            customer.id,
            connection.id,
            mask_identifier(login_id),
        )

        return Response({
            "message": "Bank connected successfully. Syncing data...",
            "status": "SYNCING"
        }, status=status.HTTP_200_OK)


class CustomerPortalBankingStatusView(CustomerPortalBaseView):
    def get(self, request):
        customer = self.get_customer(request)
        connection = customer.bank_connections.filter(is_active=True).order_by('-created_at').first()
        latest_failure = customer.activities.filter(
            metadata__reason_code=UNSUPPORTED_IBV_REASON_CODE,
        ).order_by('-created_at').first()
        failure_reason_code = (
            latest_failure.metadata.get('reason_code')
            if latest_failure and isinstance(latest_failure.metadata, dict)
            else None
        )

        payload = {
            'banking_verified': customer.banking_verified,
            'onboarding_stage': customer.onboarding_stage,
            'has_connection': connection is not None,
            'connection_status': connection.sync_status if connection else None,
            'last_synced_at': connection.last_synced_at if connection else None,
            'account_count': customer.bank_accounts.filter(connection__is_active=True).count(),
            'failure_message': (
                connection.sync_error
                if connection and connection.sync_status == 'failed'
                else latest_failure.description if latest_failure else None
            ),
            'failure_reason_code': failure_reason_code,
            'requires_ibv_refill': (
                not customer.banking_verified
                and customer.onboarding_stage == 'banking_verification'
                and failure_reason_code == UNSUPPORTED_IBV_REASON_CODE
            ),
        }

        serializer = CustomerPortalBankingStatusSerializer(payload)
        return Response(serializer.data)


class CustomerPortalBankAccountsView(CustomerPortalBaseView):
    def get(self, request):
        customer = self.get_customer(request)
        accounts = customer.bank_accounts.filter(
            connection__is_active=True
        ).prefetch_related(
            Prefetch(
                'transactions',
                queryset=BankTransaction.objects.order_by('-date', '-created_at'),
            )
        ).order_by('-is_primary', 'name')
        serializer = BankAccountSerializer(accounts, many=True)
        return Response(serializer.data)


class BankConnectionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BankConnectionSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        ordered_transactions = BankTransaction.objects.order_by('-date', '-created_at')
        queryset = BankConnection.objects.select_related('customer').filter(is_active=True).prefetch_related(
            Prefetch(
                'accounts',
                queryset=BankAccount.objects.order_by('-is_primary', 'name').prefetch_related(
                    Prefetch('transactions', queryset=ordered_transactions)
                ),
            )
        )

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset.order_by('-created_at')


class BankAccountViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BankAccountSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        ordered_transactions = BankTransaction.objects.order_by('-date', '-created_at')
        queryset = BankAccount.objects.select_related('customer', 'connection').filter(
            connection__is_active=True
        ).prefetch_related(
            Prefetch('transactions', queryset=ordered_transactions)
        )

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset.order_by('-is_primary', 'name')

    @action(detail=True, methods=['patch'], url_path='coordinates')
    def update_coordinates(self, request, pk=None):
        """Staff override institution / transit / account # from a void cheque."""
        account = self.get_object()
        serializer = BankAccountManualCoordinatesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        previous = {
            'institution_number': account.institution_number,
            'transit_number': account.transit_number,
            'account_number': account.account_number,
        }

        account.institution_number = data['institution_number']
        account.transit_number = data['transit_number']
        account.account_number = data['account_number']
        account.is_manual_entry = True
        update_fields = [
            'institution_number',
            'transit_number',
            'account_number',
            'is_manual_entry',
            'updated_at',
        ]
        if data.get('name'):
            account.name = data['name']
            update_fields.append('name')
        account.save(update_fields=update_fields)

        _refresh_unlocked_loan_snapshots(account)

        notes = (data.get('notes') or '').strip()
        description = (
            f"Updated bank coordinates from void cheque. "
            f"Institution {previous['institution_number'] or '—'}→{account.institution_number}, "
            f"Transit {previous['transit_number'] or '—'}→{account.transit_number}, "
            f"Account {previous['account_number'] or '—'}→{account.account_number}."
        )
        if notes:
            description = f"{description} Notes: {notes}"
        _log_bank_coords_activity(
            account.customer,
            title='Bank details updated (void cheque)',
            description=description,
            user=request.user,
            metadata={
                'bank_account_id': str(account.id),
                'previous': previous,
                'current': {
                    'institution_number': account.institution_number,
                    'transit_number': account.transit_number,
                    'account_number': account.account_number,
                },
            },
        )

        return Response(BankAccountSerializer(account).data)

    @action(detail=False, methods=['post'], url_path='manual')
    def create_manual(self, request):
        """Create a bank account from void-cheque details emailed to staff."""
        serializer = ManualBankAccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            customer = Customer.objects.get(id=data['customer_id'])
        except Customer.DoesNotExist:
            return Response({'error': 'Customer not found.'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            connection = _get_or_create_manual_connection(customer)
            if data.get('set_as_primary', True):
                BankAccount.objects.filter(customer=customer, is_primary=True).update(is_primary=False)

            external_id = (
                f"manual-{data['institution_number']}-"
                f"{data['transit_number']}-{data['account_number']}"
            )
            account, created = BankAccount.objects.update_or_create(
                connection=connection,
                external_id=external_id,
                defaults={
                    'customer': customer,
                    'name': data.get('name') or 'Manual / Void Cheque',
                    'type': 'checking',
                    'currency': 'CAD',
                    'institution_number': data['institution_number'],
                    'transit_number': data['transit_number'],
                    'account_number': data['account_number'],
                    'is_primary': data.get('set_as_primary', True),
                    'is_manual_entry': True,
                    'use_for_eft_funding': True,
                    'use_for_eft_collections': True,
                },
            )
            if not created:
                account.institution_number = data['institution_number']
                account.transit_number = data['transit_number']
                account.account_number = data['account_number']
                account.is_manual_entry = True
                if data.get('name'):
                    account.name = data['name']
                if data.get('set_as_primary', True):
                    account.is_primary = True
                account.save()

            _refresh_unlocked_loan_snapshots(account)

        notes = (data.get('notes') or '').strip()
        description = (
            f"{'Created' if created else 'Updated'} manual bank account from void cheque: "
            f"{account.institution_number} / {account.transit_number} / {account.account_number}."
        )
        if notes:
            description = f"{description} Notes: {notes}"
        _log_bank_coords_activity(
            customer,
            title='Manual bank account (void cheque)',
            description=description,
            user=request.user,
            metadata={'bank_account_id': str(account.id), 'created': created},
        )

        return Response(
            BankAccountSerializer(account).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class BankTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BankTransactionSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        queryset = BankTransaction.objects.select_related('customer', 'account').filter(
            account__connection__is_active=True
        )

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        account_id = self.request.query_params.get('account_id')
        if account_id:
            queryset = queryset.filter(account_id=account_id)

        return queryset.order_by('-date', '-created_at')


class FinancialAnalysisReportViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = FinancialAnalysisReportSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        queryset = FinancialAnalysisReport.objects.select_related('customer')

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset.order_by('-generated_at')


class FlinksWebhookView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        logger.info('Legacy Flinks webhook received keys=%s', sorted(request.data.keys()))
        return Response({"message": "Webhook received"}, status=status.HTTP_200_OK)
