from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import MohawkStaff, configured, enabled
from .models import DashboardSnapshot


class AvailabilityView(APIView):
    permission_classes = [MohawkStaff]

    def get(self, request):
        response = Response({'enabled': enabled(), 'configured': configured()})
        response['Cache-Control'] = 'no-store, private'
        return response


class DashboardView(APIView):
    permission_classes = [MohawkStaff]

    def get(self, request):
        if not enabled():
            response = Response({'enabled': False, 'detail': 'iCollector is disabled in Django admin.'}, status=403)
        else:
            snapshot = DashboardSnapshot.objects.using('default').filter(pk=1).first()
            fetched_at = snapshot.fetched_at if snapshot else None
            response = Response({
                'enabled': True, 'available': bool(fetched_at),
                'stale': not fetched_at or (timezone.now() - fetched_at).total_seconds() > 90,
                'fetched_at': fetched_at, 'poll_seconds': 30,
                'error': snapshot.last_error if snapshot else '',
                'dashboard': snapshot.payload if fetched_at else None,
            })
        response['Cache-Control'] = 'no-store, private'
        return response
