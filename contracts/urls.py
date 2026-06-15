from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'contracts', views.ContractViewSet, basename='contract')
router.register(r'templates', views.ContractTemplateViewSet, basename='contract-template')

urlpatterns = [
    path('', include(router.urls)),
]