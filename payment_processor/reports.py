from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from rest_framework import serializers
from .models import SyncRow


class Filters(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)
    search = serializers.CharField(required=False, max_length=150, allow_blank=True)
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    timezone = serializers.CharField(default='America/Toronto')
    flow = serializers.CharField(required=False, max_length=50)
    method = serializers.CharField(required=False, max_length=50)
    status = serializers.CharField(required=False, max_length=80)
    currency = serializers.CharField(required=False, max_length=8)
    user_id = serializers.UUIDField(required=False)
    period = serializers.ChoiceField(choices=['day', 'week', 'month'], default='day')

    def validate(self, attrs):
        if attrs.get('start') and attrs.get('end') and attrs['start'] > attrs['end']:
            raise serializers.ValidationError('Start date must not follow end date.')
        try:
            ZoneInfo(attrs['timezone'])
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError('Select a valid timezone.')
        return attrs


def filtered_rows(run, filters):
    qs = SyncRow.objects.using('default').filter(run=run)
    zone = ZoneInfo(filters['timezone'])
    for field in ('flow', 'method', 'status', 'currency', 'user_id'):
        if filters.get(field):
            qs = qs.filter(**{field: str(filters[field])})
    if filters.get('start'):
        qs = qs.filter(created_at__gte=datetime.combine(filters['start'], time.min, zone))
    if filters.get('end'):
        qs = qs.filter(created_at__lt=datetime.combine(filters['end'] + timedelta(days=1), time.min, zone))
    if filters.get('search'):
        term = filters['search']
        qs = qs.filter(Q(provider_id__icontains=term) | Q(user_name__icontains=term) |
                       Q(payload__Memo__icontains=term) | Q(payload__Comment__icontains=term) |
                       Q(payload__Email__icontains=term) | Q(payload__FirstName__icontains=term) |
                       Q(payload__LastName__icontains=term) | Q(payload__Name__icontains=term))
    return qs


def metrics():
    return {'count': 0, 'amount': Decimal('0'), 'completed_count': 0, 'completed_amount': Decimal('0'),
            'failed_count': 0, 'failed_amount': Decimal('0'), 'cancelled_count': 0,
            'pending_count': 0, 'maximum': Decimal('0'), 'duration_seconds': Decimal('0'), 'duration_count': 0}


def accumulate(group, row):
    amount = row.amount or Decimal('0')
    group['count'] += 1
    group['amount'] += amount
    group['maximum'] = max(group['maximum'], amount)
    status = row.status.casefold().replace(' ', '')
    if status == 'completed':
        group['completed_count'] += 1
        group['completed_amount'] += amount
    elif status in ('failed', 'rejected'):
        group['failed_count'] += 1
        group['failed_amount'] += amount
    elif status in ('cancelled', 'canceled'):
        group['cancelled_count'] += 1
    else:
        group['pending_count'] += 1
    if row.completed_at and row.created_at and row.completed_at >= row.created_at:
        group['duration_seconds'] += Decimal(str((row.completed_at - row.created_at).total_seconds()))
        group['duration_count'] += 1


def render(group):
    result = {key: str(value.quantize(Decimal('.01'))) if isinstance(value, Decimal) else value
              for key, value in group.items() if key not in ('duration_seconds', 'duration_count')}
    result['average'] = str((group['amount'] / group['count']).quantize(Decimal('.01'))) if group['count'] else '0.00'
    result['success_rate'] = str((Decimal(group['completed_count']) * 100 / group['count']).quantize(Decimal('.01'))) if group['count'] else None
    result['average_processing_seconds'] = str((group['duration_seconds'] / group['duration_count']).quantize(Decimal('.01'))) if group['duration_count'] else None
    result['processing_time_samples'] = group['duration_count']
    return result


def report_data(qs, filters):
    groups = {name: defaultdict(metrics) for name in ('totals', 'flows', 'methods', 'statuses', 'periods', 'failures', 'users', 'returns')}
    zone = ZoneInfo(filters['timezone'])
    for row in qs.iterator(chunk_size=1000):
        currency = row.currency or 'UNKNOWN'
        keys = {'totals': currency, 'flows': (currency, row.flow), 'methods': (currency, row.method), 'statuses': (currency, row.status)}
        if row.created_at:
            date = row.created_at.astimezone(zone).date()
            if filters['period'] == 'week':
                date -= timedelta(days=date.weekday())
            elif filters['period'] == 'month':
                date = date.replace(day=1)
            keys['periods'] = (currency, date.isoformat())
        if row.failure:
            keys['failures'] = (currency, row.failure)
        if row.user_id or row.user_name:
            keys['users'] = (currency, row.user_id, row.user_name)
        if 'return' in row.failure.casefold() or 'refund' in row.flow.casefold() or 'revers' in row.flow.casefold():
            keys['returns'] = (currency, row.flow, row.failure)
        for category, key in keys.items():
            accumulate(groups[category][key], row)
    result = {}
    for name, values in groups.items():
        items = []
        for key, value in sorted(values.items()):
            key = (key,) if isinstance(key, str) else key
            total = groups['totals'][key[0]]
            items.append({'currency': key[0], 'label': key[1] if len(key) > 1 else key[0],
                          **({'name': key[2]} if len(key) > 2 else {}), **render(value),
                          'volume_share': str((value['amount'] * 100 / total['amount']).quantize(Decimal('.01'))) if total['amount'] else None,
                          'count_share': str((Decimal(value['count']) * 100 / total['count']).quantize(Decimal('.01'))) if total['count'] else None})
        if name == 'users':
            items.sort(key=lambda item: Decimal(item['amount']), reverse=True)
        result[name] = items
    return result
