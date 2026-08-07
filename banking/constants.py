"""Institution-number rules shared by IBV ingestion and Zūm Rails money movement."""

# Legacy: previously auto-deleted Flinks connections for 621/623. Empty so IBV
# saves those banks like any other; agents decline with "Unsupported bank" if needed.
UNSUPPORTED_IBV_INSTITUTIONS = frozenset()

# Institutions that need extra agent review before funding/collections.
# Agents may still proceed after verifying PAD / payment history (not a hard block).
PAYMENT_RISK_INSTITUTIONS = frozenset({'621', '623', '703'})
# Backward-compatible alias used across the codebase / serializers.
PAYMENT_BLOCKED_INSTITUTIONS = PAYMENT_RISK_INSTITUTIONS

PAYMENT_RISK_WARNING_MESSAGE = (
    'Problematic bank (institution {institution}) — please verify that the client '
    'has PAD payments or sufficient payment history in the account before proceeding.'
)
# Backward-compatible alias for older imports / tests.
PAYMENT_BLOCKED_INSTITUTION_MESSAGE = PAYMENT_RISK_WARNING_MESSAGE


def normalize_institution_number(raw_value):
    """Return the trailing 3 digits of an institution number, or '' when unknown."""
    value = ''.join(ch for ch in str(raw_value or '') if ch.isdigit())
    return value[-3:] if len(value) >= 3 else value


def is_payment_blocked_institution(raw_value) -> bool:
    """True when the institution is in the agent-review risk set (621/623/703)."""
    return normalize_institution_number(raw_value) in PAYMENT_RISK_INSTITUTIONS


def payment_blocked_message(raw_value) -> str:
    return payment_risk_warning_message(raw_value)


def payment_risk_warning_message(raw_value) -> str:
    return PAYMENT_RISK_WARNING_MESSAGE.format(
        institution=normalize_institution_number(raw_value) or 'unknown'
    )
