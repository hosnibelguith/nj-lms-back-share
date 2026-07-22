# accounts/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from .arrive_views import (
    ArriveCreateLeadView,
    ArriveHandoffExchangeView,
    ArrivePortalSessionView,
)

router = DefaultRouter()
router.register(r'users', views.UserViewSet, basename='user')
router.register(r'customers', views.CustomerViewSet, basename='customer')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/csrf/', views.CsrfTokenView.as_view(), name='csrf-token'),
    path('auth/login/', views.LoginView.as_view(), name='login'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/me/', views.CurrentUserView.as_view(), name='current-user'),
    path('auth/refresh/', views.RefreshTokenView.as_view(), name='refresh-token'),
    
    # Customer Portal Routes
    path('portal/apply/', views.CustomerApplyView.as_view(), name='customer-apply'),
    path('portal/set-password/', views.CustomerPasswordSetupView.as_view(), name='customer-password-setup'),
    path('portal/signup/start/', views.CustomerSignupStartView.as_view(), name='customer-signup-start'),
    path('portal/signup/verify-phone/', views.CustomerSignupVerifyPhoneView.as_view(), name='customer-signup-verify-phone'),
    path('portal/password-reset/request/', views.CustomerPasswordResetRequestView.as_view(), name='customer-password-reset-request'),
    path('portal/password-reset/verify/', views.CustomerPasswordResetVerifyView.as_view(), name='customer-password-reset-verify'),
    path('portal/password-reset/confirm/', views.CustomerPasswordResetConfirmView.as_view(), name='customer-password-reset-confirm'),
    path(
        'portal/login/request-otp/',
        views.CustomerPortalRequestOTPView.as_view(),
        name='customer-login-request-otp',
    ),
    path(
        'portal/login/verify-otp/',
        views.CustomerPortalVerifyOTPView.as_view(),
        name='customer-login-verify-otp',
    ),
    path('portal/login/', views.CustomerPortalLoginView.as_view(), name='customer-portal-login'),
    path('portal/me/', views.CustomerPortalMeView.as_view(), name='customer-portal-me'),
    path('portal/me/loans/', views.CustomerPortalMyLoansView.as_view(), name='customer-portal-my-loans'),
    path(
        'portal/me/current-application/',
        views.CustomerPortalCurrentApplicationView.as_view(),
        name='customer-portal-current-application',
    ),
    path(
        'portal/me/dashboard/',
        views.CustomerPortalDashboardView.as_view(),
        name='customer-portal-dashboard',
    ),
    path(
        'portal/me/contract-preview/',
        views.CustomerPortalContractPreviewView.as_view(),
        name='customer-portal-contract-preview',
    ),
    path(
        'portal/me/sign-contract/',
        views.CustomerPortalSignContractView.as_view(),
        name='customer-portal-sign-contract',
    ),
    path(
        'portal/me/job-references/',
        views.CustomerPortalJobReferencesView.as_view(),
        name='customer-portal-job-references',
    ),
    path(
        'portal/me/run-analysis/',
        views.CustomerPortalRunAnalysisView.as_view(),
        name='customer-portal-run-analysis',
    ),
    path('portal/me/integrations/', views.ApiIntegrationsView.as_view(), name='api-integrations'),
    path('portal/arrive/handoff/', ArriveHandoffExchangeView.as_view(), name='arrive-handoff-exchange'),
    path('integrations/arrive/leads/', ArriveCreateLeadView.as_view(), name='arrive-create-lead'),
    path(
        'integrations/arrive/portal-session/',
        ArrivePortalSessionView.as_view(),
        name='arrive-portal-session',
    ),
]
