# communications/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'communications', views.CommunicationViewSet, basename='communication')
router.register(r'templates', views.CommunicationTemplateViewSet, basename='communication-template')

urlpatterns = [
    path('', include(router.urls)),
    path('webhooks/twilio/', views.TwilioWebhookView.as_view(), name='twilio-webhook'),
    path('send-email/', views.SendEmailView.as_view(), name='send-email'),
    path('send-sms/', views.SendSMSView.as_view(), name='send-sms'),
]
