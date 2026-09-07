"""Early renewal eligibility and net-disbursement math.

Landing/organic collecting loans with a clean payment history can open a new
loan near the end of the current term. At funding, the old balance is taken
from the new principal so the client only receives the difference.

Remaining-payment window (automatic eligibility)
-----------------------------------------------
Eligible remaining installments are 1 through ``max_remaining`` inclusive.

``max_remaining`` is:
  * long-loan value (default **2**) when the loan is longer or larger:
      original planned payments >= 6
      OR duration days >= 84  (6 x 14-day bi-weekly)
      OR principal >= $1000
  * short-loan value (default **1**) otherwise

Typical $500 / 4-pay loans therefore qualify on the last payment only.
6+ payment or $1000+ loans qualify with 1 or 2 remaining.

Adjust without a code change via GlobalSetting:
  EARLY_RENEWAL_SHORT_MAX_REMAINING     default 1
  EARLY_RENEWAL_LONG_MAX_REMAINING      default 2
  EARLY_RENEWAL_LONG_MIN_PAYMENTS       default 6
  EARLY_RENEWAL_LONG_MIN_DURATION_DAYS  default 84
  EARLY_RENEWAL_LONG_MIN_PRINCIPAL      default 1000.00

To restore “any loan with 1 or 2 remaining”:
  set SHORT_MAX_REMAINING=2 (or LONG_MIN_PAYMENTS=1, LONG_MIN_DURATION_DAYS=0,
  LONG_MIN_PRINCIPAL=0).

Clean history (automatic eligibility)
-------------------------------------
No missed, NSF, returned, failed, or stop payment on the current loan.
Pending/processing collections also block until they settle.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from accounts.models import Customer
from loans.models import Loan


REMAINING_PAYMENT_STATUSES = ('scheduled', 'unscheduled')
DIRTY_PAYMENT_STATUSES = ('nsf', 'failed')
DIRTY_COLLECTION_STATUSES = ('failed', 'returned', 'rejected')
IN_PROGRESS_APPLICATION_STATUSES = (
    'ibv_pending',
    'pending',
    'pending_signature',
    'pending_funding',
)
EARLY_RENEWAL_TEMPLATE_NAME = 'Early Renewal Offer'

SETTING_SHORT_MAX_REMAINING = 'EARLY_RENEWAL_SHORT_MAX_REMAINING'
SETTING_LONG_MAX_REMAINING = 'EARLY_RENEWAL_LONG_MAX_REMAINING'
SETTING_LONG_MIN_PAYMENTS = 'EARLY_RENEWAL_LONG_MIN_PAYMENTS'
SETTING_LONG_MIN_DURATION_DAYS = 'EARLY_RENEWAL_LONG_MIN_DURATION_DAYS'
SETTING_LONG_MIN_PRINCIPAL = 'EARLY_RENEWAL_LONG_MIN_PRINCIPAL'

DEFAULT_SHORT_MAX_REMAINING = 1
DEFAULT_LONG_MAX_REMAINING = 2
DEFAULT_LONG_MIN_PAYMENTS = 6
DEFAULT_LONG_MIN_DURATION_DAYS = 84
DEFAULT_LONG_MIN_PRINCIPAL = Decimal('1000.00')


def money(value) -> Decimal:
    from loans.services import LoanService

    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return LoanService.money(value)


def _setting_raw(key, default):
    from accounts.models import GlobalSetting

    raw = GlobalSetting.get_value(key, default)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip()


def _setting_int(key, default: int, *, minimum=0, maximum=52) -> int:
    try:
        value = int(_setting_raw(key, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _setting_decimal(key, default: Decimal) -> Decimal:
    try:
        value = money(_setting_raw(key, str(default)))
    except (TypeError, ValueError, InvalidOperation):
        return money(default)
    if value < 0:
        return money(default)
    return value


def remaining_window_settings() -> dict:
    short_max = _setting_int(
        SETTING_SHORT_MAX_REMAINING,
        DEFAULT_SHORT_MAX_REMAINING,
        minimum=1,
        maximum=12,
    )
    long_max = _setting_int(
        SETTING_LONG_MAX_REMAINING,
        DEFAULT_LONG_MAX_REMAINING,
        minimum=1,
        maximum=12,
    )
    if long_max < short_max:
        long_max = short_max
    return {
        'short_max_remaining': short_max,
        'long_max_remaining': long_max,
        'long_min_payments': _setting_int(
            SETTING_LONG_MIN_PAYMENTS,
            DEFAULT_LONG_MIN_PAYMENTS,
            minimum=1,
            maximum=52,
        ),
        'long_min_duration_days': _setting_int(
            SETTING_LONG_MIN_DURATION_DAYS,
            DEFAULT_LONG_MIN_DURATION_DAYS,
            minimum=0,
            maximum=3650,
        ),
        'long_min_principal': _setting_decimal(
            SETTING_LONG_MIN_PRINCIPAL,
            DEFAULT_LONG_MIN_PRINCIPAL,
        ),
    }


def is_arrive_customer(customer) -> bool:
    return bool(
        getattr(customer, 'source', None) == Customer.SOURCE_ARRIVE
        or getattr(customer, 'arrive_application_id', None)
    )


def remaining_payment_count(loan: Loan) -> int:
    return loan.payments.filter(status__in=REMAINING_PAYMENT_STATUSES).count()


def original_planned_payments(loan: Loan) -> int:
    from loans.services import LoanService

    formula = getattr(loan, 'formula', None)
    formula_n = int(getattr(formula, 'default_number_of_payments', 0) or 0)
    live = 0
    for payment in loan.payments.exclude(status='cancelled'):
        if LoanService.is_non_installment_fee_payment(payment):
            continue
        live += 1
    return max(live, formula_n)


def loan_duration_days(loan: Loan) -> int:
    from loans.services import LoanService

    cadence = int(LoanService._schedule_frequency_days(loan) or 14)
    if cadence <= 0:
        cadence = 14
    return original_planned_payments(loan) * cadence


def remaining_window_for_loan(loan: Loan) -> dict:
    settings = remaining_window_settings()
    planned = original_planned_payments(loan)
    duration_days = loan_duration_days(loan)
    principal = money(loan.principal or Decimal('0.00'))
    is_long = (
        planned >= settings['long_min_payments']
        or duration_days >= settings['long_min_duration_days']
        or principal >= settings['long_min_principal']
    )
    max_remaining = (
        settings['long_max_remaining'] if is_long else settings['short_max_remaining']
    )
    remaining = remaining_payment_count(loan)
    return {
        **settings,
        'remaining_payments': remaining,
        'original_planned_payments': planned,
        'duration_days': duration_days,
        'window': 'long' if is_long else 'short',
        'max_remaining': max_remaining,
        'in_window': 1 <= remaining <= max_remaining,
    }


def has_dirty_payment_history(loan: Loan) -> bool:
    """True when this loan has a missed, NSF, returned, failed, or stop payment."""
    from loans.collection_policy import classify_failure_reason

    if loan.payments.filter(status__in=DIRTY_PAYMENT_STATUSES).exists():
        return True
    if loan.collection_payments.filter(status__in=DIRTY_COLLECTION_STATUSES).exists():
        return True
    for payment in loan.payments.exclude(failure_reason='').exclude(failure_reason=None):
        if classify_failure_reason(payment.failure_reason) == 'stop_payment':
            return True
    for collection in loan.collection_payments.exclude(
        failure_reason=''
    ).exclude(failure_reason=None):
        if classify_failure_reason(collection.failure_reason) == 'stop_payment':
            return True
    return False


def early_renewal_ineligible_reason(loan: Loan) -> str | None:
    if loan.status != 'active':
        return 'Early renewal is only available on an active collecting loan.'
    if is_arrive_customer(loan.customer):
        return 'Early renewal is not available for Arrive applications.'
    if loan.payments.filter(status='pending').exists() or loan.collection_payments.filter(
        status='processing'
    ).exists():
        return 'Early renewal is unavailable while a collection is processing.'
    if has_dirty_payment_history(loan):
        return (
            'Early renewal requires a clean payment history on this loan '
            '(no missed, NSF, returned, failed, or stop payments).'
        )
    window = remaining_window_for_loan(loan)
    remaining = window['remaining_payments']
    max_remaining = window['max_remaining']
    if remaining < 1:
        return 'Early renewal is only available while payments are still remaining.'
    if remaining > max_remaining:
        if max_remaining == 1:
            return 'Early renewal for this loan is available with 1 payment left.'
        return (
            f'Early renewal for this loan is available with 1 to {max_remaining} '
            'payments left.'
        )
    if money(loan.balance or Decimal('0.00')) <= 0:
        return 'There is no remaining balance to renew against.'
    return None


def is_early_renewal_eligible(loan: Loan) -> bool:
    return early_renewal_ineligible_reason(loan) is None


def eligible_early_renewal_loan(customer: Customer):
    if is_arrive_customer(customer):
        return None
    if customer.loans.filter(status__in=IN_PROGRESS_APPLICATION_STATUSES).exists():
        return None
    loan = (
        customer.loans.filter(status='active')
        .order_by('-created_at')
        .first()
    )
    if loan is None or not is_early_renewal_eligible(loan):
        return None
    return loan


def suggested_renewal_principal(loan: Loan) -> Decimal:
    requested = getattr(loan.customer, 'requested_loan_amount', None)
    if requested:
        amount = money(requested)
        if amount > 0:
            return amount
    return money(loan.principal or Decimal('0.00'))


def early_renewal_offer(loan: Loan, new_principal=None) -> dict:
    old_balance = money(loan.balance or Decimal('0.00'))
    principal = (
        money(new_principal)
        if new_principal is not None
        else suggested_renewal_principal(loan)
    )
    net = money(principal - old_balance)
    net_to_client = net if net > 0 else Decimal('0.00')
    window = remaining_window_for_loan(loan)
    return {
        'eligible': is_early_renewal_eligible(loan),
        'remaining_payments': window['remaining_payments'],
        'max_remaining': window['max_remaining'],
        'window': window['window'],
        'old_loan_id': str(loan.id),
        'old_balance': str(old_balance),
        'remaining_balance': str(old_balance),
        'amount_deducted': str(old_balance),
        'new_loan_amount': str(principal),
        'net_to_client': str(net_to_client),
        'new_amount_covers_balance': net > 0,
    }


def renewal_payoff_amount(loan: Loan) -> Decimal:
    previous = getattr(loan, 'previous_loan', None)
    if previous is None:
        return Decimal('0.00')
    stored = getattr(loan, 'renewal_payoff_amount', None)
    if stored is not None:
        return money(stored)
    return money(previous.balance or Decimal('0.00'))


def disbursement_amount(loan: Loan) -> Decimal:
    principal = money(loan.principal or Decimal('0.00'))
    payoff = renewal_payoff_amount(loan)
    net = money(principal - payoff)
    if net <= 0:
        raise ValueError(
            'New loan amount must be greater than the old loan balance.'
        )
    return net
