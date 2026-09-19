import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='loans.CollectionPayment', dispatch_uid='icollector.capture_reject')
def capture_reject(sender, instance, using, raw=False, **kwargs):
    if raw or using != 'default':
        return
    if instance.status not in ('failed', 'returned', 'rejected') and not any(
        value in (instance.zum_status or '').casefold() for value in ('failed', 'returned', 'rejected')):
        return
    # A savepoint isolates a plugin database error from the loan transaction.
    # Periodic reconciliation recovers missed captures from committed source rows.
    try:
        from .services import enqueue
        with transaction.atomic(using='default'):
            enqueue(instance.pk)
    except Exception as exc:
        logger.warning('iCollector reject capture deferred (%s)', type(exc).__name__)
