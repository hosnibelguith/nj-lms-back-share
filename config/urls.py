# config/urls.py
"""
URL configuration for LendStack project.
"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

# Import views from apps
from accounts.views import LoginView, LogoutView, CurrentUserView, RefreshTokenView, UserViewSet, CustomerViewSet
from banking.views import (
    BankConnectionViewSet, BankAccountViewSet, BankTransactionViewSet,
    FinancialAnalysisReportViewSet, FlinksWebhookView, ConnectBankView,
    CustomerPortalBankingStatusView, CustomerPortalBankAccountsView
)
from loans.views import FundingMethodRecommendationViewSet, LoanViewSet, PaymentViewSet
from loans.webhooks import ZumRailsWebhookView
from contracts.views import ContractViewSet, ContractTemplateViewSet
from communications.views import (
    CommunicationViewSet, CommunicationTemplateViewSet, TwilioWebhookView
)
from activity.views import ActivityHistoryViewSet, CommentViewSet

# Create router
router = DefaultRouter()

# Account routes
router.register(r'users', UserViewSet, basename='user')
router.register(r'customers', CustomerViewSet, basename='customer')

# Banking routes
router.register(r'bank-connections', BankConnectionViewSet, basename='bank-connection')
router.register(r'bank-accounts', BankAccountViewSet, basename='bank-account')
router.register(r'bank-transactions', BankTransactionViewSet, basename='bank-transaction')
router.register(r'financial-reports', FinancialAnalysisReportViewSet, basename='financial-report')

# Loan routes
router.register(r'loans', LoanViewSet, basename='loan')
router.register(r'payments', PaymentViewSet, basename='payment')
router.register(r'funding-method-recommendations', FundingMethodRecommendationViewSet, basename='funding-method-recommendation')

# Contract routes
router.register(r'contracts', ContractViewSet, basename='contract')
router.register(r'contract-templates', ContractTemplateViewSet, basename='contract-template')

# Communication routes
router.register(r'communications', CommunicationViewSet, basename='communication')
router.register(r'communication-templates', CommunicationTemplateViewSet, basename='communication-template')

# Activity routes
router.register(r'activities', ActivityHistoryViewSet, basename='activity')
router.register(r'comments', CommentViewSet, basename='comment')

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),
    path('api/', include('accounts.urls')),

    # Banking endpoints
    path('api/banking/connect/', ConnectBankView.as_view(), name='bank-connect'),
    path('api/portal/me/banking/', CustomerPortalBankingStatusView.as_view(), name='customer-portal-banking-status'),
    path('api/portal/me/bank-accounts/', CustomerPortalBankAccountsView.as_view(), name='customer-portal-bank-accounts'),
    # API routes
    path('api/', include(router.urls)),
    
    # Webhook endpoints
    path('api/webhooks/flinks/', FlinksWebhookView.as_view(), name='flinks-webhook'),
    path('api/webhooks/twilio/', TwilioWebhookView.as_view(), name='twilio-webhook'),
    path('api/webhooks/zumrails/', ZumRailsWebhookView.as_view(), name='zumrails-webhook'),
    
    # Health check for Heroku
    path('health/', lambda r: __import__('django.http', fromlist=['JsonResponse']).JsonResponse({'status': 'ok'}), name='health'),
]
