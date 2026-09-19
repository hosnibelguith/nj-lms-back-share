from django.urls import path
from .views import AvailabilityView, DashboardView

urlpatterns = [
    path('availability/', AvailabilityView.as_view(), name='icollector-availability'),
    path('dashboard/', DashboardView.as_view(), name='icollector-dashboard'),
]
