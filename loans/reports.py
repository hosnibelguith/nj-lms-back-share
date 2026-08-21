"""Staff Report Center — only report types this LMS actually stores."""
from decimal import Decimal

from django.db.models import Count, Exists, Min, OuterRef, Q, Sum

from accounts.models import Customer, User
from activity.models import ActivityHistory, Comment
from banking.models import BankConnection
from communications.models import Communication
from contracts.models import Contract

from .models import FundedPayment, Loan, Payment
from .services import LoanService

MAX_ROWS = 2000
ALERT_ACTIVITY_TYPES = ('payment_failed', 'loan_defaulted', 'customer_blocked')
REAL_PAYMENT_STATUSES = ('completed',)
ACTIVE_LOAN_STATUS = 'active'


def _money(value):
    if value is None:
        return '0.00'
    return str(value)


def _iso(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _customer_name(customer):
    if customer is None:
        return ''
    return f'{(customer.first_name or "").strip()} {(customer.last_name or "").strip()}'.strip()


def _source_value(source):
    value = (source or '').strip().lower()
    return value if value in ('arrive', 'organic') else None


def _filter_datetime(qs, field, date_from, date_to):
    if date_from:
        qs = qs.filter(**{f'{field}__date__gte': date_from})
    if date_to:
        qs = qs.filter(**{f'{field}__date__lte': date_to})
    return qs


def _filter_date(qs, field, date_from, date_to):
    if date_from:
        qs = qs.filter(**{f'{field}__gte': date_from})
    if date_to:
        qs = qs.filter(**{f'{field}__lte': date_to})
    return qs


def _apply_source(qs, source, lookup='customer__source'):
    source_value = _source_value(source)
    if source_value:
        qs = qs.filter(**{lookup: source_value})
    return qs


def _filter_loan_list_dates(qs, date_from, date_to, *, prefix=''):
    """Match the loans page: funded date when present, otherwise created date."""
    funded = f'{prefix}funded_at' if prefix else 'funded_at'
    created = f'{prefix}created_at' if prefix else 'created_at'
    if date_from:
        qs = qs.filter(
            Q(**{f'{funded}__date__gte': date_from})
            | Q(**{f'{funded}__isnull': True, f'{created}__date__gte': date_from})
        )
    if date_to:
        qs = qs.filter(
            Q(**{f'{funded}__date__lte': date_to})
            | Q(**{f'{funded}__isnull': True, f'{created}__date__lte': date_to})
        )
    return qs


def _funding_loan_id_ref(prefix=''):
    return OuterRef('loan_id') if prefix else OuterRef('pk')


def _failed_funding_exists(prefix=''):
    return FundedPayment.objects.filter(
        loan_id=_funding_loan_id_ref(prefix),
        status__in=('failed', 'returned', 'cancelled'),
    )


def _active_funding_exists(prefix=''):
    return FundedPayment.objects.filter(
        loan_id=_funding_loan_id_ref(prefix),
        status__in=('processing', 'completed'),
    )


def _apply_loan_status_filter(qs, status, *, prefix=''):
    status = (status or '').strip()
    if not status or status == 'all':
        return qs
    status_field = f'{prefix}status' if prefix else 'status'
    customer = f'{prefix}customer' if prefix else 'customer'
    signed_at = f'{prefix}contract_signed_at' if prefix else 'contract_signed_at'
    if status == 'approved_pending_signature':
        return qs.filter(**{status_field: 'pending_funding'}).filter(
            Q(**{f'{signed_at}__isnull': True}),
            Q(**{f'{customer}__contract_completed': False}),
        )
    if status == 'pending_funding':
        qs = qs.filter(**{status_field: 'pending_funding'}).filter(
            Q(**{f'{signed_at}__isnull': False})
            | Q(**{f'{customer}__contract_completed': True})
        )
        return qs.exclude(Exists(_active_funding_exists(prefix)))
    if status == 'funding_failed':
        qs = qs.filter(**{status_field: 'pending_funding'}).filter(
            Exists(_failed_funding_exists(prefix))
        )
        return qs.exclude(Exists(_active_funding_exists(prefix)))
    return qs.filter(**{status_field: status})


def _apply_loan_staff_filters(qs, extra, *, prefix=''):
    """Province / status / AI / IBV / source — same meaning as the loans page."""
    extra = extra or {}
    customer = f'{prefix}customer' if prefix else 'customer'
    status_field = f'{prefix}status' if prefix else 'status'
    ai_field = f'{prefix}ai_decision' if prefix else 'ai_decision'

    source = (extra.get('source') or '').strip().lower()
    if source == 'arrive':
        qs = qs.filter(
            Q(**{f'{customer}__source': 'arrive'})
            | (
                Q(**{f'{customer}__arrive_application_id__isnull': False})
                & ~Q(**{f'{customer}__arrive_application_id': ''})
            )
        )
    elif source in ('organic', 'landing', 'kyc'):
        qs = qs.exclude(**{f'{customer}__source': 'arrive'}).filter(
            Q(**{f'{customer}__arrive_application_id__isnull': True})
            | Q(**{f'{customer}__arrive_application_id': ''})
        )

    province = (extra.get('province') or '').strip()
    if province:
        qs = qs.filter(**{f'{customer}__province': province})

    qs = _apply_loan_status_filter(qs, extra.get('status'), prefix=prefix)

    ai_decision = (extra.get('ai_decision') or '').strip()
    if ai_decision:
        qs = qs.filter(**{ai_field: ai_decision})

    ibv_status = (extra.get('ibv_status') or '').strip()
    if ibv_status == 'pending':
        qs = qs.filter(
            Q(**{status_field: 'ibv_pending'})
            | Q(**{f'{customer}__banking_verified': False})
        )
    elif ibv_status == 'completed':
        qs = qs.filter(**{f'{customer}__banking_verified': True})
    return qs


def _queryset_summary(qs, *, category_field, amount_field=None, amount_label=None):
    count = qs.count()
    amount_total = None
    if amount_field:
        amount_total = format(
            Decimal(str(qs.aggregate(total=Sum(amount_field))['total'] or Decimal('0.00'))),
            '.2f',
        )
    category_counts = [
        {'value': row[category_field], 'count': row['count']}
        for row in qs.values(category_field)
        .annotate(count=Count('id'))
        .order_by('-count', category_field)
    ]
    return {
        'row_count': count,
        'amount_key': amount_field,
        'amount_label': amount_label,
        'amount_total': amount_total,
        'category_key': category_field,
        'category_counts': category_counts,
    }


def _slice_rows(qs, row_fn):
    count = qs.count()
    truncated = count > MAX_ROWS
    rows = [row_fn(item) for item in qs[:MAX_ROWS]]
    return count, truncated, rows


def _cols(*pairs):
    return [{'key': key, 'label': label} for key, label in pairs]


def build_customer_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    qs = _filter_datetime(Customer.objects.all(), 'created_at', date_from, date_to)
    source_val = (extra.get('source') or '').strip().lower()
    if source_val == 'arrive':
        qs = qs.filter(
            Q(source='arrive')
            | (Q(arrive_application_id__isnull=False) & ~Q(arrive_application_id=''))
        )
    elif source_val in ('organic', 'landing', 'kyc'):
        qs = qs.exclude(source='arrive').filter(
            Q(arrive_application_id__isnull=True) | Q(arrive_application_id='')
        )
    province = (extra.get('province') or '').strip()
    if province:
        qs = qs.filter(province=province)
    ibv_status = (extra.get('ibv_status') or '').strip()
    if ibv_status == 'pending':
        qs = qs.filter(banking_verified=False)
    elif ibv_status == 'completed':
        qs = qs.filter(banking_verified=True)
    qs = qs.order_by('-created_at', 'id')

    def row(customer):
        return {
            'customer_id': str(customer.id),
            'customer_name': _customer_name(customer),
            'email': customer.email or '',
            'phone': customer.phone or '',
            'province': customer.province or '',
            'status': customer.status,
            'source': customer.source,
            'sms_opted_out': customer.sms_opted_out,
            'created_at': _iso(customer.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('customer_id', 'Customer ID'),
        ('customer_name', 'Customer'),
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('province', 'Province'),
        ('status', 'Status'),
        ('source', 'Source'),
        ('sms_opted_out', 'SMS opted out'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_loan_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    qs = _apply_loan_staff_filters(
        _filter_loan_list_dates(
            Loan.objects.select_related('customer'),
            date_from,
            date_to,
        ),
        extra,
    ).order_by('-created_at', 'id')
    summary = _queryset_summary(
        qs,
        category_field='status',
        amount_field='principal',
        amount_label='Principal',
    )

    def row(loan):
        return {
            'loan_id': str(loan.id),
            'customer_name': _customer_name(loan.customer),
            'customer_email': loan.customer.email or '',
            'status': loan.status,
            'source': loan.customer.source,
            'principal': _money(loan.principal),
            'fee': _money(loan.fee),
            'total_amount': _money(loan.total_amount),
            'balance': _money(loan.balance),
            'funded_at': _iso(loan.funded_at),
            'created_at': _iso(loan.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('customer_email', 'Email'),
        ('status', 'Status'),
        ('source', 'Source'),
        ('principal', 'Principal'),
        ('fee', 'Fee'),
        ('total_amount', 'Total'),
        ('balance', 'Balance'),
        ('funded_at', 'Funded'),
        ('created_at', 'Created'),
    ), count, truncated, rows, summary


def build_payment_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    if not (extra.get('status') or '').strip() or extra.get('status') == 'all':
        extra['status'] = ACTIVE_LOAN_STATUS
    qs = _apply_loan_staff_filters(
        _filter_date(
            Payment.objects.select_related('loan__customer').filter(
                status__in=REAL_PAYMENT_STATUSES,
            ),
            'scheduled_date',
            date_from,
            date_to,
        ),
        extra,
        prefix='loan__',
    ).order_by('-scheduled_date', '-created_at', 'id')
    summary = _queryset_summary(
        qs,
        category_field='type',
        amount_field='amount',
        amount_label='Amount',
    )

    def row(payment):
        return {
            'payment_id': str(payment.id),
            'loan_id': str(payment.loan_id),
            'customer_name': _customer_name(payment.loan.customer),
            'loan_status': payment.loan.status,
            'amount': _money(payment.amount),
            'type': payment.type,
            'status': payment.status,
            'scheduled_date': _iso(payment.scheduled_date),
            'original_date': _iso(payment.original_date),
            'processed_at': _iso(payment.processed_at),
            'notes': payment.notes or '',
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('payment_id', 'Payment ID'),
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('loan_status', 'Loan status'),
        ('amount', 'Amount'),
        ('type', 'Type'),
        ('status', 'Status'),
        ('scheduled_date', 'Scheduled'),
        ('original_date', 'Original date'),
        ('processed_at', 'Processed'),
        ('notes', 'Notes'),
    ), count, truncated, rows, summary


def build_payment_plan_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    qs = _apply_loan_staff_filters(
        _filter_loan_list_dates(
            Loan.objects.select_related('customer').annotate(
                payment_count=Count('payments'),
                next_due=Min(
                    'payments__scheduled_date',
                    filter=Q(payments__status='scheduled'),
                ),
                scheduled_remaining=Sum(
                    'payments__amount',
                    filter=Q(payments__status='scheduled'),
                ),
            ),
            date_from,
            date_to,
        ),
        extra,
    ).order_by('-created_at', 'id')

    def row(loan):
        return {
            'loan_id': str(loan.id),
            'customer_name': _customer_name(loan.customer),
            'status': loan.status,
            'balance': _money(loan.balance),
            'schedule_frequency': loan.schedule_frequency or '',
            'payment_count': loan.payment_count,
            'scheduled_remaining': _money(loan.scheduled_remaining),
            'next_due': _iso(loan.next_due),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('status', 'Status'),
        ('balance', 'Balance'),
        ('schedule_frequency', 'Frequency'),
        ('payment_count', 'Payments'),
        ('scheduled_remaining', 'Scheduled remaining'),
        ('next_due', 'Next due'),
    ), count, truncated, rows


def build_contract_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            Contract.objects.select_related('customer', 'loan'),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(contract):
        return {
            'contract_id': str(contract.id),
            'loan_id': str(contract.loan_id),
            'customer_name': _customer_name(contract.customer),
            'status': contract.status,
            'typed_name': contract.typed_name or '',
            'signed_date': _iso(contract.signed_date),
            'created_at': _iso(contract.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('contract_id', 'Contract ID'),
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('status', 'Status'),
        ('typed_name', 'Signed name'),
        ('signed_date', 'Signed'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_communication_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            Communication.objects.select_related('customer', 'loan'),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(item):
        return {
            'communication_id': str(item.id),
            'customer_name': _customer_name(item.customer),
            'type': item.type,
            'direction': item.direction,
            'status': item.status,
            'subject': item.subject or '',
            'to': item.to_address or item.to_phone or '',
            'template_name': item.template_name or '',
            'created_at': _iso(item.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('communication_id', 'ID'),
        ('customer_name', 'Customer'),
        ('type', 'Type'),
        ('direction', 'Direction'),
        ('status', 'Status'),
        ('subject', 'Subject'),
        ('to', 'To'),
        ('template_name', 'Template'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_notification_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            Communication.objects.select_related('customer').filter(
                Q(type='notification') | Q(direction='outbound'),
            ),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(item):
        return {
            'communication_id': str(item.id),
            'customer_name': _customer_name(item.customer),
            'type': item.type,
            'status': item.status,
            'subject': item.subject or '',
            'to': item.to_address or item.to_phone or '',
            'created_at': _iso(item.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('communication_id', 'ID'),
        ('customer_name', 'Customer'),
        ('type', 'Type'),
        ('status', 'Status'),
        ('subject', 'Subject'),
        ('to', 'To'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_automation_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            Communication.objects.select_related('customer').exclude(
                Q(template_name__isnull=True) | Q(template_name=''),
            ),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(item):
        return {
            'communication_id': str(item.id),
            'customer_name': _customer_name(item.customer),
            'type': item.type,
            'status': item.status,
            'template_name': item.template_name,
            'subject': item.subject or '',
            'created_at': _iso(item.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('communication_id', 'ID'),
        ('customer_name', 'Customer'),
        ('type', 'Type'),
        ('status', 'Status'),
        ('template_name', 'Template'),
        ('subject', 'Subject'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_bank_verification_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            BankConnection.objects.select_related('customer'),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(connection):
        return {
            'connection_id': str(connection.id),
            'customer_name': _customer_name(connection.customer),
            'customer_email': connection.customer.email or '',
            'provider': connection.provider,
            'sync_status': connection.sync_status,
            'is_active': connection.is_active,
            'last_synced_at': _iso(connection.last_synced_at),
            'created_at': _iso(connection.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('connection_id', 'Connection ID'),
        ('customer_name', 'Customer'),
        ('customer_email', 'Email'),
        ('provider', 'Provider'),
        ('sync_status', 'Sync status'),
        ('is_active', 'Active'),
        ('last_synced_at', 'Last synced'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_fees_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    qs = _apply_loan_staff_filters(
        _filter_date(
            Payment.objects.select_related('loan__customer').filter(
                Q(notes__startswith='Deferral fee')
                | Q(notes__startswith=LoanService.COLLECTION_FAILURE_FEE_NOTE)
            ),
            'scheduled_date',
            date_from,
            date_to,
        ),
        extra,
        prefix='loan__',
    ).order_by('-scheduled_date', '-created_at', 'id')

    rows = []
    for payment in qs[: MAX_ROWS + 1]:
        if LoanService.is_deferral_fee_payment(payment):
            fee_type = 'deferral'
        elif LoanService.is_collection_failure_fee_payment(payment):
            fee_type = 'nsf'
        else:
            continue
        rows.append({
            'payment_id': str(payment.id),
            'loan_id': str(payment.loan_id),
            'customer_name': _customer_name(payment.loan.customer),
            'fee_type': fee_type,
            'amount': _money(payment.amount),
            'status': payment.status,
            'scheduled_date': _iso(payment.scheduled_date),
            'notes': payment.notes or '',
        })
        if len(rows) > MAX_ROWS:
            break

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return _cols(
        ('payment_id', 'Payment ID'),
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('fee_type', 'Fee type'),
        ('amount', 'Amount'),
        ('status', 'Status'),
        ('scheduled_date', 'Scheduled'),
        ('notes', 'Notes'),
    ), len(rows) if not truncated else MAX_ROWS, truncated, rows


def build_interest_report(date_from, date_to, source, extra=None):
    extra = dict(extra or {})
    extra.setdefault('source', source)
    qs = _apply_loan_staff_filters(
        _filter_loan_list_dates(
            Loan.objects.select_related('customer', 'formula').prefetch_related('payments'),
            date_from,
            date_to,
        ),
        extra,
    ).order_by('-created_at', 'id')

    def row(loan):
        money = LoanService.money
        principal = loan.principal or Decimal('0.00')
        formula = loan.formula
        if formula and principal:
            brokerage = money(principal * formula.brokerage_percent / Decimal('100'))
        else:
            brokerage = Decimal('0.00')
        planned = money(max((loan.fee or Decimal('0.00')) - brokerage, Decimal('0.00')))
        extra = Decimal('0.00')
        for payment in loan.payments.all():
            if LoanService.is_collection_failure_interest_payment(payment):
                extra += payment.amount or Decimal('0.00')
        return {
            'loan_id': str(loan.id),
            'customer_name': _customer_name(loan.customer),
            'status': loan.status,
            'principal': _money(principal),
            'brokerage_fee': _money(brokerage),
            'planned_interest': _money(planned),
            'collection_failure_interest': _money(money(extra)),
            'total_fee': _money(loan.fee),
            'created_at': _iso(loan.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('loan_id', 'Loan ID'),
        ('customer_name', 'Customer'),
        ('status', 'Status'),
        ('principal', 'Principal'),
        ('brokerage_fee', 'Brokerage'),
        ('planned_interest', 'Planned interest'),
        ('collection_failure_interest', 'NSF extra interest'),
        ('total_fee', 'Total fee'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_leads_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(Customer.objects.all(), 'created_at', date_from, date_to),
        source,
        lookup='source',
    ).order_by('-created_at', 'id')

    def row(customer):
        latest_loan = customer.loans.order_by('-created_at').first()
        return {
            'customer_id': str(customer.id),
            'customer_name': _customer_name(customer),
            'email': customer.email or '',
            'source': customer.source,
            'arrive_application_id': customer.arrive_application_id or '',
            'onboarding_stage': customer.onboarding_stage,
            'loan_status': latest_loan.status if latest_loan else '',
            'created_at': _iso(customer.created_at),
        }

    count, truncated, rows = _slice_rows(qs.prefetch_related('loans'), row)
    return _cols(
        ('customer_id', 'Customer ID'),
        ('customer_name', 'Customer'),
        ('email', 'Email'),
        ('source', 'Source'),
        ('arrive_application_id', 'Arrive application'),
        ('onboarding_stage', 'Onboarding'),
        ('loan_status', 'Latest loan'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_notes_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            Comment.objects.select_related('customer', 'loan', 'created_by'),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(comment):
        author = ''
        if comment.created_by_id:
            author = comment.created_by.full_name or comment.created_by.email or ''
        return {
            'note_id': str(comment.id),
            'customer_name': _customer_name(comment.customer),
            'loan_id': str(comment.loan_id) if comment.loan_id else '',
            'content': comment.content,
            'is_internal': comment.is_internal,
            'created_by': author,
            'created_at': _iso(comment.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('note_id', 'Note ID'),
        ('customer_name', 'Customer'),
        ('loan_id', 'Loan ID'),
        ('content', 'Note'),
        ('is_internal', 'Internal'),
        ('created_by', 'Author'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_opt_in_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(Customer.objects.all(), 'created_at', date_from, date_to),
        source,
        lookup='source',
    ).order_by('-created_at', 'id')

    def row(customer):
        return {
            'customer_id': str(customer.id),
            'customer_name': _customer_name(customer),
            'email': customer.email or '',
            'phone': customer.phone or '',
            'sms_opted_in': not customer.sms_opted_out,
            'sms_opted_out': customer.sms_opted_out,
            'sms_opted_out_at': _iso(customer.sms_opted_out_at),
            'sms_opt_out_reason': customer.sms_opt_out_reason or '',
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('customer_id', 'Customer ID'),
        ('customer_name', 'Customer'),
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('sms_opted_in', 'SMS opted in'),
        ('sms_opted_out', 'SMS opted out'),
        ('sms_opted_out_at', 'Opted out at'),
        ('sms_opt_out_reason', 'Reason'),
    ), count, truncated, rows


def build_customer_alert_report(date_from, date_to, source, extra=None):
    qs = _apply_source(
        _filter_datetime(
            ActivityHistory.objects.select_related('customer', 'loan').filter(
                type__in=ALERT_ACTIVITY_TYPES,
            ),
            'created_at',
            date_from,
            date_to,
        ),
        source,
    ).order_by('-created_at', 'id')

    def row(item):
        return {
            'activity_id': str(item.id),
            'customer_name': _customer_name(item.customer),
            'loan_id': str(item.loan_id) if item.loan_id else '',
            'type': item.type,
            'title': item.title,
            'description': item.description,
            'created_at': _iso(item.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('activity_id', 'Activity ID'),
        ('customer_name', 'Customer'),
        ('loan_id', 'Loan ID'),
        ('type', 'Type'),
        ('title', 'Title'),
        ('description', 'Description'),
        ('created_at', 'Created'),
    ), count, truncated, rows


def build_employee_report(date_from, date_to, source, extra=None):
    del source
    qs = _filter_datetime(
        User.objects.filter(user_type='staff'),
        'created_at',
        date_from,
        date_to,
    ).order_by('-created_at', 'id')

    def row(user):
        return {
            'user_id': str(user.id),
            'full_name': user.full_name,
            'email': user.email,
            'permission_level': user.permission_level,
            'is_active': user.is_active,
            'created_at': _iso(user.created_at),
        }

    count, truncated, rows = _slice_rows(qs, row)
    return _cols(
        ('user_id', 'User ID'),
        ('full_name', 'Name'),
        ('email', 'Email'),
        ('permission_level', 'Permission'),
        ('is_active', 'Active'),
        ('created_at', 'Created'),
    ), count, truncated, rows


REPORT_SPECS = (
    {
        'id': 'automation',
        'label': 'Automation Report',
        'description': 'Messages sent from templates (reminders, funded, failed payment).',
        'builder': build_automation_report,
    },
    {
        'id': 'bank_verification',
        'label': 'Bank Verifications Report',
        'description': 'Flinks / IBV bank connections and sync status.',
        'builder': build_bank_verification_report,
    },
    {
        'id': 'communication',
        'label': 'Communication Report',
        'description': 'Email and SMS history.',
        'builder': build_communication_report,
    },
    {
        'id': 'contract',
        'label': 'Contracts Report',
        'description': 'Loan agreements drafted or signed in-app.',
        'builder': build_contract_report,
    },
    {
        'id': 'customer',
        'label': 'Customer Report',
        'description': 'Customer records and status.',
        'builder': build_customer_report,
    },
    {
        'id': 'customer_alert',
        'label': 'Customer Alerts Report',
        'description': 'Failed payment, default, and blocked-account activity.',
        'builder': build_customer_alert_report,
    },
    {
        'id': 'opt_in',
        'label': 'Customer Opt Ins Report',
        'description': 'SMS opt-in / opt-out status.',
        'builder': build_opt_in_report,
    },
    {
        'id': 'employee',
        'label': 'Employee Report',
        'description': 'Staff users and permission levels.',
        'builder': build_employee_report,
    },
    {
        'id': 'fees',
        'label': 'Fees Report',
        'description': '$35 deferral fees and $50 NSF / collection-failure fees.',
        'builder': build_fees_report,
    },
    {
        'id': 'interest',
        'label': 'Interest Report',
        'description': 'Loan brokerage, planned interest, and NSF extra interest.',
        'builder': build_interest_report,
    },
    {
        'id': 'leads',
        'label': 'Leads Report',
        'description': 'Arrive and landing applications.',
        'builder': build_leads_report,
    },
    {
        'id': 'loan',
        'label': 'Loan Report',
        'description': 'Loans with amounts, status, and funding dates.',
        'builder': build_loan_report,
    },
    {
        'id': 'notes',
        'label': 'Notes Report',
        'description': 'Internal staff comments on customers and loans.',
        'builder': build_notes_report,
    },
    {
        'id': 'notification',
        'label': 'Notifications Report',
        'description': 'Outbound notifications and messages.',
        'builder': build_notification_report,
    },
    {
        'id': 'payment',
        'label': 'Payment Report',
        'description': 'Completed payments on active loans.',
        'builder': build_payment_report,
    },
    {
        'id': 'payment_plan',
        'label': 'Payment Plans Report',
        'description': 'Per-loan schedule remaining and next due date.',
        'builder': build_payment_plan_report,
    },
)

REPORT_BY_ID = {spec['id']: spec for spec in REPORT_SPECS}
REPORT_TYPE_IDS = frozenset(REPORT_BY_ID)


def list_report_types():
    return [
        {
            'id': spec['id'],
            'label': spec['label'],
            'description': spec['description'],
        }
        for spec in REPORT_SPECS
    ]


def run_report(
    report_type,
    *,
    date_from=None,
    date_to=None,
    source=None,
    province=None,
    status=None,
    ai_decision=None,
    ibv_status=None,
):
    spec = REPORT_BY_ID[report_type]
    extra = {
        'source': source,
        'province': province,
        'status': status,
        'ai_decision': ai_decision,
        'ibv_status': ibv_status,
    }
    result = spec['builder'](date_from, date_to, source, extra)
    summary = None
    if len(result) == 5:
        columns, count, truncated, rows, summary = result
    else:
        columns, count, truncated, rows = result
    payload = {
        'report_type': spec['id'],
        'label': spec['label'],
        'columns': columns,
        'count': count,
        'truncated': truncated,
        'results': rows,
    }
    if summary:
        payload['summary'] = summary
    return payload
