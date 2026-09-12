from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q, Value
from django.db.models.functions import Lower, Replace
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
    outcome = serializers.ChoiceField(choices=['completed', 'failed', 'cancelled', 'pending'], required=False, allow_blank=True)
    direction = serializers.ChoiceField(choices=['in', 'out'], required=False, allow_blank=True)
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
    if run.resource == 'transactions':
        qs = qs.exclude(Q(method__icontains='card') | Q(method__iexact='VisaDirect') |
                        Q(flow__icontains='card') | Q(flow__iexact='StatementPayment'))
    zone = ZoneInfo(filters['timezone'])
    for field in ('flow', 'method', 'status', 'currency', 'user_id'):
        if filters.get(field):
            qs = qs.filter(**{field: str(filters[field])})
    if filters.get('direction'):
        qs = qs.filter(flow__in={'in': ['AccountsReceivable', 'FundZumWallet'],
                                'out': ['AccountsPayable', 'WithdrawZumWallet']}[filters['direction']])
    if filters.get('outcome'):
        statuses = {'completed': ['completed'], 'failed': ['failed', 'rejected'], 'cancelled': ['cancelled', 'canceled']}
        qs = qs.annotate(_outcome_status=Lower(Replace('status', Value(' '), Value(''))))
        if filters['outcome'] == 'pending':
            qs = qs.exclude(_outcome_status__in=sum(statuses.values(), []))
        else:
            qs = qs.filter(_outcome_status__in=statuses[filters['outcome']])
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
            'failed_count': 0, 'failed_amount': Decimal('0'), 'cancelled_count': 0, 'cancelled_amount': Decimal('0'),
            'pending_count': 0, 'pending_amount': Decimal('0'), 'maximum': Decimal('0'), 'duration_seconds': Decimal('0'), 'duration_count': 0}


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
        group['cancelled_amount'] += amount
    else:
        group['pending_count'] += 1
        group['pending_amount'] += amount
    if row.completed_at and row.created_at and row.completed_at >= row.created_at:
        group['duration_seconds'] += Decimal(str((row.completed_at - row.created_at).total_seconds()))
        group['duration_count'] += 1


def render(group):
    result = {key: str(value.quantize(Decimal('.01'))) if isinstance(value, Decimal) else value
              for key, value in group.items() if key not in ('duration_seconds', 'duration_count')}
    result['average'] = str((group['amount'] / group['count']).quantize(Decimal('.01'))) if group['count'] else '0.00'
    result['completed_average'] = str((group['completed_amount'] / group['completed_count']).quantize(Decimal('.01'))) if group['completed_count'] else None
    result['success_rate'] = str((Decimal(group['completed_count']) * 100 / group['count']).quantize(Decimal('.01'))) if group['count'] else None
    result['average_processing_seconds'] = str((group['duration_seconds'] / group['duration_count']).quantize(Decimal('.01'))) if group['duration_count'] else None
    result['processing_time_samples'] = group['duration_count']
    return result


def report_data(qs, filters):
    groups = {name: defaultdict(metrics) for name in ('totals', 'flows', 'methods', 'statuses', 'periods', 'failures', 'users', 'returns')}
    flow_periods = defaultdict(lambda: defaultdict(metrics))
    ap_statuses = defaultdict(metrics)
    ap_users = defaultdict(metrics)
    undated = defaultdict(int)
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
            accumulate(flow_periods[(currency, date.isoformat())][row.flow], row)
        else:
            undated[currency] += 1
        if row.flow == 'AccountsPayable':
            accumulate(ap_statuses[(currency, row.status)], row)
            if row.user_id or row.user_name:
                accumulate(ap_users[(currency, row.user_id, row.user_name)], row)
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
    result['dashboard'] = {
        'currencies': [dict(currency=currency,
                            **movement({flow: value for (code, flow), value in groups['flows'].items() if code == currency}),
                            undated_count=undated[currency]) for currency in sorted(groups['totals'])],
        'trend': [dict(currency=currency, date=date, **movement(flows))
                  for (currency, date), flows in sorted(flow_periods.items())],
        'ap_statuses': [dict(currency=currency, label=status, **render(value))
                        for (currency, status), value in sorted(ap_statuses.items())],
        'ap_users': [dict(currency=currency, label=user_id, name=name, **render(value))
                     for (currency, user_id, name), value in sorted(ap_users.items())],
        'date_basis': 'CreatedAt',
        'movement_basis': 'Completed transactions: AR collections plus wallet funding in; AP payments plus wallet withdrawals out.',
    }
    return result


def movement(flows):
    """Completed provider transaction movement, never wallet balance or ledger totals."""
    names = {'ap': 'AccountsPayable', 'ar': 'AccountsReceivable',
             'wallet_funding': 'FundZumWallet', 'wallet_withdrawal': 'WithdrawZumWallet'}
    values = {}
    for label, flow in names.items():
        group = flows.get(flow, metrics())
        values[label + '_amount'] = group['completed_amount']
        values[label + '_count'] = group['completed_count']
    for suffix in ('amount', 'count'):
        values['in_' + suffix] = values['ar_' + suffix] + values['wallet_funding_' + suffix]
        values['out_' + suffix] = values['ap_' + suffix] + values['wallet_withdrawal_' + suffix]
    values['net_amount'] = values['in_amount'] - values['out_amount']
    recognized = set(names.values())
    values['unclassified_completed_count'] = sum(group['completed_count'] for flow, group in flows.items() if flow not in recognized)
    values['unclassified_completed_amount'] = sum((group['completed_amount'] for flow, group in flows.items() if flow not in recognized), Decimal('0'))
    return {key: str(value.quantize(Decimal('.01'))) if isinstance(value, Decimal) else value for key, value in values.items()}
