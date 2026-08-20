"""Owner-controlled collection stop policy and problematic-account flags.

Account-closed failures still auto-stop. NSF / stop-payment / other reasons
either stay on Active for staff review, or auto-stop after N missed collections
when the owner enables that setting.
"""
COLLECTION_AUTO_STOP_MODE_KEY = 'COLLECTION_AUTO_STOP_MODE'
COLLECTION_AUTO_STOP_MISSED_COUNT_KEY = 'COLLECTION_AUTO_STOP_MISSED_COUNT'
AUTO_STOP_MODE_MANUAL = 'manual'
AUTO_STOP_MODE_AFTER_MISSED = 'after_missed'
DEFAULT_AUTO_STOP_MODE = AUTO_STOP_MODE_AFTER_MISSED
DEFAULT_AUTO_STOP_MISSED_COUNT = 3
NSF_PROBLEM_THRESHOLD = 3
STOP_PAYMENT_PROBLEM_THRESHOLD = 2
OTHER_PROBLEM_THRESHOLD = 2
MIXED_PROBLEM_THRESHOLD = 3


def _setting_value(key, default):
    from accounts.models import GlobalSetting

    raw = GlobalSetting.get_value(key, default)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip()


def auto_stop_mode() -> str:
    mode = _setting_value(COLLECTION_AUTO_STOP_MODE_KEY, DEFAULT_AUTO_STOP_MODE).lower()
    if mode == AUTO_STOP_MODE_MANUAL:
        return AUTO_STOP_MODE_MANUAL
    return AUTO_STOP_MODE_AFTER_MISSED


def auto_stop_missed_count() -> int:
    raw = _setting_value(
        COLLECTION_AUTO_STOP_MISSED_COUNT_KEY,
        str(DEFAULT_AUTO_STOP_MISSED_COUNT),
    )
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_STOP_MISSED_COUNT
    return max(1, min(count, 20))


def classify_failure_reason(reason: str | None) -> str:
    text = (reason or '').lower().replace('_', ' ').replace('-', ' ')
    compact = text.replace(' ', '')
    if 'accountclosed' in compact or 'closedaccount' in compact:
        return 'account_closed'
    if 'stoppayment' in compact or 'stop pay' in text:
        return 'stop_payment'
    if 'nsf' in compact or 'insufficient' in compact:
        return 'nsf'
    return 'other'


def is_account_closed_reason(reason: str | None) -> bool:
    return classify_failure_reason(reason) == 'account_closed'


def loan_failure_counts(loan) -> dict:
    counts = {
        'nsf': 0,
        'stop_payment': 0,
        'account_closed': 0,
        'other': 0,
        'total': 0,
    }
    collections = loan.collection_payments.filter(status__in=['failed', 'returned'])
    for collection in collections:
        kind = classify_failure_reason(collection.failure_reason)
        counts[kind] += 1
        counts['total'] += 1
    return counts


def problematic_reasons(counts: dict) -> list[str]:
    labels = []
    if counts.get('account_closed'):
        labels.append('account closed')
    if counts.get('nsf', 0) >= NSF_PROBLEM_THRESHOLD:
        labels.append(f"{counts['nsf']} NSF")
    if counts.get('stop_payment', 0) >= STOP_PAYMENT_PROBLEM_THRESHOLD:
        labels.append(f"{counts['stop_payment']} stop payment")
    if counts.get('other', 0) >= OTHER_PROBLEM_THRESHOLD:
        labels.append(f"{counts['other']} other returned")
    if (
        not labels
        and counts.get('total', 0) >= MIXED_PROBLEM_THRESHOLD
    ):
        labels.append(f"{counts['total']} returned collections")
    return labels


def is_problematic_counts(counts: dict) -> bool:
    return bool(problematic_reasons(counts))


def should_auto_stop_loan(loan, reason: str | None) -> bool:
    """True when this failure should mark the loan defaulted / stopped."""
    if is_account_closed_reason(reason):
        return True
    if auto_stop_mode() != AUTO_STOP_MODE_AFTER_MISSED:
        return False
    counts = loan_failure_counts(loan)
    return counts['total'] >= auto_stop_missed_count()


def collection_risk_payload(loan) -> dict:
    counts = loan_failure_counts(loan)
    reasons = problematic_reasons(counts)
    return {
        'is_problematic': bool(reasons),
        'risk_label': (
            f'This is a problematic account ({", ".join(reasons)})'
            if reasons
            else ''
        ),
        'nsf_count': counts['nsf'],
        'stop_payment_count': counts['stop_payment'],
        'account_closed_count': counts['account_closed'],
        'returned_count': counts['total'],
    }


def settings_payload() -> dict:
    return {
        'mode': auto_stop_mode(),
        'missed_count': auto_stop_missed_count(),
        'account_closed_always_stops': True,
        'nsf_warning_count': NSF_PROBLEM_THRESHOLD,
        'stop_payment_warning_count': STOP_PAYMENT_PROBLEM_THRESHOLD,
    }


def save_settings(*, mode: str, missed_count: int | None = None) -> dict:
    from accounts.models import GlobalSetting

    normalized = (
        AUTO_STOP_MODE_MANUAL
        if str(mode).strip().lower() == AUTO_STOP_MODE_MANUAL
        else AUTO_STOP_MODE_AFTER_MISSED
    )
    GlobalSetting.objects.update_or_create(
        key=COLLECTION_AUTO_STOP_MODE_KEY,
        defaults={
            'value': normalized,
            'description': 'manual = staff stop loans; after_missed = auto-stop after N returned collections',
        },
    )
    if missed_count is not None:
        count = max(1, min(int(missed_count), 20))
        GlobalSetting.objects.update_or_create(
            key=COLLECTION_AUTO_STOP_MISSED_COUNT_KEY,
            defaults={
                'value': str(count),
                'description': 'Auto-stop a loan after this many returned collections (when mode is after_missed)',
            },
        )
    return settings_payload()


def annotate_problematic_loans(loan_ids):
    """Return {loan_id: risk_payload} for the given loans."""
    from .models import Loan

    payload = {}
    loans = (
        Loan.objects.filter(id__in=list(loan_ids))
        .prefetch_related('collection_payments')
    )
    for loan in loans:
        payload[str(loan.id)] = collection_risk_payload(loan)
    return payload
