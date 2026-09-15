"""Durable delivery of existing rejection records; never operates a payment provider."""
import logging
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .access import configuration, enabled
from .models import DashboardSnapshot, RejectDelivery

logger = logging.getLogger(__name__)
TORONTO = ZoneInfo('America/Toronto')


def rejection_query():
    return (Q(status__in=['failed', 'returned', 'rejected'])
        | Q(zum_status__icontains='Failed') | Q(zum_status__iexact='Returned')
        | Q(zum_status__iexact='Rejected')) & ~Q(status='cancelled')


def source_rejects():
    from loans.models import CollectionPayment
    return CollectionPayment.objects.using('default').filter(
        rejection_query(), loan__customer__lender__slug='mohawkloans',
    ).select_related('loan__customer').annotate(
        effective_returned_at=Coalesce('returned_at', 'updated_at', 'initiated_at'))


def enqueue(source_id, *, dispatch=True):
    if not enabled():
        return False
    source = source_rejects().filter(pk=source_id).first()
    if source is None:
        return False
    delivery, created = RejectDelivery.objects.using('default').get_or_create(
        source_id=source.pk, defaults={'lender_id': source.loan.customer.lender_id})
    if created and dispatch:
        transaction.on_commit(lambda: finish_capture(delivery.pk), using='default')
    return created


def freeze_payload(source_id):
    source = source_rejects().filter(pk=source_id).first()
    if source is not None:
        RejectDelivery.objects.using('default').filter(pk=source_id, payload={}).update(
            payload=payload_for(source))


def finish_capture(source_id):
    try:
        freeze_payload(source_id)
    except Exception as exc:
        logger.warning('iCollector snapshot capture deferred (%s)', type(exc).__name__)
    kick_delivery(source_id)


def capture_final_failure(source_id):
    """Called after balance and fee adjustments, within the failure transaction."""
    try:
        if enabled():
            with transaction.atomic(using='default'):
                enqueue(source_id)
                freeze_payload(source_id)
    except Exception as exc:
        logger.warning('iCollector final capture deferred (%s)', type(exc).__name__)


def kick_delivery(source_id):
    try:
        from .tasks import send_reject
        send_reject.apply_async(args=[str(source_id)], retry=False)
    except Exception as exc:
        # The outbox remains durable even when Redis is unavailable.
        logger.warning('iCollector task dispatch deferred (%s)', type(exc).__name__)


def reconcile():
    config = configuration()
    if not config or not config.enabled or not config.capture_since:
        return 0
    existing = RejectDelivery.objects.using('default').values('source_id')
    ids = list(source_rejects().filter(Q(effective_returned_at__gte=config.capture_since)
        | Q(updated_at__gte=config.capture_since))
        .exclude(pk__in=existing).order_by('effective_returned_at').values_list('pk', flat=True)[:250])
    return sum(enqueue(pk, dispatch=False) for pk in ids)


def payload_for(source):
    from accounts.utils.phone import normalize_ca_phone
    from rest_framework.exceptions import ValidationError
    customer = source.loan.customer
    try:
        phone = normalize_ca_phone(customer.phone or '')
    except ValidationError:
        # Freeze financial facts even when contact data needs correction.
        phone = customer.phone or ''
    name = f'{customer.first_name or ""} {customer.last_name or ""}'.strip()
    returned = source.returned_at or source.updated_at or source.initiated_at
    return {'idempotency_key': str(source.pk), 'data': {
        'Client name': name or customer.email or '',
        'Email': customer.email or '', 'Phone': phone,
        'Reason': source.failure_reason or '',
        'Missed payment': float(source.amount), 'Balance': float(source.loan.balance),
        'Returned date': timezone.localtime(returned, TORONTO).date().isoformat(),
    }}


def retry_corrected(source_id):
    """Refresh contact fields for a blocked row without rewriting financial facts."""
    with transaction.atomic(using='default'):
        row = RejectDelivery.objects.using('default').select_for_update().filter(pk=source_id, status='blocked').first()
        source = source_rejects().filter(pk=source_id).first()
        if row is None or source is None:
            return False
        corrected = payload_for(source)
        original = row.payload.get('data', {})
        for key in ('Missed payment', 'Balance', 'Returned date'):
            if key in original:
                corrected['data'][key] = original[key]
        row.payload, row.status = corrected, 'pending'
        row.next_attempt_at, row.last_error, row.lease_until = timezone.now(), '', None
        row.save(using='default')
        return True


def deliver(source_id, client=None):
    if not enabled():
        return False
    now, lease_id = timezone.now(), uuid.uuid4()
    with transaction.atomic(using='default'):
        row = RejectDelivery.objects.using('default').select_for_update().filter(pk=source_id).first()
        if not row or row.status in ('delivered', 'cancelled', 'blocked') or row.next_attempt_at > now:
            return False
        if row.lease_until and row.lease_until > now:
            return False
        source = source_rejects().filter(pk=source_id, loan__customer__lender_id=row.lender_id).first()
        if source is None:
            row.status = 'cancelled'
            row.last_error = 'Source is no longer an eligible Mohawk reject.'
            row.save(using='default')
            return False
        if not row.payload:
            # Freeze the payload after the originating payment transaction commits.
            # Later retries use identical values even if the loan balance changes.
            row.payload = payload_for(source)
        from accounts.utils.phone import normalize_ca_phone
        from rest_framework.exceptions import ValidationError
        try:
            normalize_ca_phone(row.payload['data']['Phone'])
        except ValidationError:
            row.status = 'blocked'
            row.last_error = 'A valid Canadian phone number is required; correct the customer phone and retry.'
            row.save(using='default')
            return False
        row.status, row.lease_id = 'sending', lease_id
        row.lease_until = now + timedelta(minutes=2)
        row.attempts += 1
        row.save(using='default')
    target = RejectDelivery.objects.using('default').filter(pk=source_id, lease_id=lease_id)
    if not enabled():
        target.update(status='pending', lease_until=None)
        return False
    from .client import OceonClient, OceonError
    try:
        receipt = (client or OceonClient()).send_reject(row.payload)
    except OceonError as exc:
        delay = min(3600, 30 * 2 ** min(row.attempts - 1, 7))
        target.update(status='pending' if exc.retryable else 'blocked', lease_until=None, last_error=exc.safe_message,
            last_http_status=exc.http_status, next_attempt_at=timezone.now() + timedelta(seconds=delay))
        return False
    target.update(status='delivered', lease_until=None, delivered_at=timezone.now(),
        last_error='', last_http_status=receipt['http_status'], remote_row_id=receipt.get('row_id', ''))
    return True


def refresh_dashboard(client=None):
    if not enabled():
        return False
    now, lease_id = timezone.now(), uuid.uuid4()
    with transaction.atomic(using='default'):
        DashboardSnapshot.objects.using('default').get_or_create(pk=1)
        snapshot = DashboardSnapshot.objects.using('default').select_for_update().get(pk=1)
        if (snapshot.lease_until and snapshot.lease_until > now) or (
            snapshot.attempted_at and snapshot.attempted_at > now - timedelta(seconds=29)):
            return False
        snapshot.attempted_at = now
        snapshot.lease_id, snapshot.lease_until = lease_id, now + timedelta(minutes=2)
        snapshot.save(using='default')
    target = DashboardSnapshot.objects.using('default').filter(pk=1, lease_id=lease_id)
    if not enabled():
        target.update(lease_until=None)
        return False
    from .client import OceonClient, OceonError
    try:
        payload = (client or OceonClient()).dashboard()
    except OceonError as exc:
        target.update(last_error=exc.safe_message, lease_until=None)
        return False
    target.update(payload=payload, fetched_at=timezone.now(), last_error='', lease_until=None)
    return True
