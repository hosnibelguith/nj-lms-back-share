import logging

from rest_framework import permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .logging_utils import mask_identifier
from .models import BankConnection, BankAccount, BankTransaction, FinancialAnalysisReport
from .serializers import (
    BankConnectionSerializer,
    BankAccountSerializer,
    BankTransactionSerializer,
    FinancialAnalysisReportSerializer,
    CustomerPortalBankingStatusSerializer,
)
from .tasks import fetch_flinks_accounts_only

logger = logging.getLogger(__name__)


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

        payload = {
            'banking_verified': customer.banking_verified,
            'onboarding_stage': customer.onboarding_stage,
            'has_connection': connection is not None,
            'connection_status': connection.sync_status if connection else None,
            'last_synced_at': connection.last_synced_at if connection else None,
            'account_count': customer.bank_accounts.filter(connection__is_active=True).count(),
            'failure_message': connection.sync_error if connection and connection.sync_status == 'failed' else None,
        }

        serializer = CustomerPortalBankingStatusSerializer(payload)
        return Response(serializer.data)


class CustomerPortalBankAccountsView(CustomerPortalBaseView):
    def get(self, request):
        customer = self.get_customer(request)
        accounts = customer.bank_accounts.filter(
            connection__is_active=True
        ).prefetch_related('transactions').order_by('-is_primary', 'name')
        serializer = BankAccountSerializer(accounts, many=True)
        return Response(serializer.data)


class BankConnectionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BankConnectionSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        queryset = BankConnection.objects.select_related('customer').filter(is_active=True)

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset.order_by('-created_at')


class BankAccountViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BankAccountSerializer
    permission_classes = [permissions.IsAuthenticated, StaffOnlyPermission]

    def get_queryset(self):
        queryset = BankAccount.objects.select_related('customer', 'connection').filter(
            connection__is_active=True
        )

        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)

        return queryset.order_by('-is_primary', 'name')


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
