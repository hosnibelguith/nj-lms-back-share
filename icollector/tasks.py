from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .access import enabled
from .models import RejectDelivery
from .services import deliver, reconcile, refresh_dashboard


@shared_task(ignore_result=True)
def send_reject(source_id):
    return deliver(source_id)


@shared_task(ignore_result=True)
def tick():
    if not enabled():
        return
    reconcile()
    refresh_dashboard()
    now = timezone.now()
    ids = RejectDelivery.objects.using('default').filter(
        status__in=['pending', 'sending'], next_attempt_at__lte=now,
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    for source_id in list(ids.order_by('next_attempt_at').values_list('source_id', flat=True)[:50]):
        send_reject.apply_async(args=[str(source_id)], retry=False)
