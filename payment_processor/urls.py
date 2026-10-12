from django.urls import path
from . import views

urlpatterns = [
    path('availability/', views.AvailabilityView.as_view()),
    path('status/', views.StatusView.as_view()),
    path('sync/', views.SyncView.as_view()),
    path('wallets/', views.WalletView.as_view()),
    path('reports/', views.ReportsView.as_view()),
    path('records/<str:resource>/', views.RecordsView.as_view()),
    path('details/<str:resource>/<uuid:provider_id>/', views.DetailView.as_view()),
    path('export/<str:resource>/', views.ExportView.as_view()),
    path('operations/', views.OperationsView.as_view()),
    path('operations/<uuid:operation_id>/execute/', views.ExecuteView.as_view()),
    path('webhooks/', views.WebhooksView.as_view()),
    path('webhook/', views.WebhookReceiveView.as_view()),
]
