# banking/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'connections', views.BankConnectionViewSet, basename='bank-connection')
router.register(r'accounts', views.BankAccountViewSet, basename='bank-account')
router.register(r'transactions', views.BankTransactionViewSet, basename='bank-transaction')
router.register(r'analysis-reports', views.FinancialAnalysisReportViewSet, basename='analysis-report')

urlpatterns = [
    path('', include(router.urls)),
    path('connect/', views.ConnectBankView.as_view(), name='bank-connect'),
    path('webhooks/flinks/', views.FlinksWebhookView.as_view(), name='flinks-webhook'),
]
