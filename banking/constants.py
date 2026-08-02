"""Institution-number rules shared by IBV ingestion and Zūm Rails money movement."""

# Institutions that make an entire IBV connection unusable (connection is deleted
# and the customer is asked to reconnect).
UNSUPPORTED_IBV_INSTITUTIONS = frozenset({'621', '623'})

# Institutions that must never be used as a Zūm Rails funding (AccountsPayable)
# or collections (AccountsReceivable) account, regardless of how the account
# entered the system (Flinks sync, manual void cheque, or Mohawk webhook).
PAYMENT_BLOCKED_INSTITUTIONS = frozenset({'621', '623', '703'})

PAYMENT_BLOCKED_INSTITUTION_MESSAGE = (
    'Institution {institution} cannot be used for funding or collections. '
    'Select a different bank account.'
)


def normalize_institution_number(raw_value):
    """Return the trailing 3 digits of an institution number, or '' when unknown."""
    value = ''.join(ch for ch in str(raw_value or '') if ch.isdigit())
    return value[-3:] if len(value) >= 3 else value


def is_payment_blocked_institution(raw_value) -> bool:
    return normalize_institution_number(raw_value) in PAYMENT_BLOCKED_INSTITUTIONS


def payment_blocked_message(raw_value) -> str:
    return PAYMENT_BLOCKED_INSTITUTION_MESSAGE.format(
        institution=normalize_institution_number(raw_value) or 'unknown'
    )
