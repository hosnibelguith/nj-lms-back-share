# loans/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'loans', views.LoanViewSet, basename='loan')
router.register(r'payments', views.PaymentViewSet, basename='payment')
router.register(r'loan-formulas', views.LoanFormulaViewSet, basename='loan-formula')

urlpatterns = [
    path('', include(router.urls)),
]
