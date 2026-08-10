"""Institution-number rules shared by IBV ingestion and Zūm Rails money movement."""

# Legacy: previously auto-deleted Flinks connections for 621/623. Empty so IBV
# saves those banks like any other; agents decline with "Unsupported bank" if needed.
UNSUPPORTED_IBV_INSTITUTIONS = frozenset()

# Institutions that need extra agent review before funding/collections.
# Agents may still proceed after verifying other lenders collected (PAD) from the account.
PAYMENT_RISK_INSTITUTIONS = frozenset({'621', '623', '703'})
# Backward-compatible alias used across the codebase / serializers.
PAYMENT_BLOCKED_INSTITUTIONS = PAYMENT_RISK_INSTITUTIONS

PAYMENT_RISK_WARNING_MESSAGE = (
    'Problematic bank (institution {institution}) — verify other lenders were able '
    'to collect from this account before proceeding.'
)
# Backward-compatible alias for older imports / tests.
PAYMENT_BLOCKED_INSTITUTION_MESSAGE = PAYMENT_RISK_WARNING_MESSAGE


def normalize_institution_number(raw_value):
    """Return the trailing 3 digits of an institution number, or '' when unknown."""
    value = ''.join(ch for ch in str(raw_value or '') if ch.isdigit())
    return value[-3:] if len(value) >= 3 else value


_PLACEHOLDER_BANK_COORDS = frozenset({
    'N/A', 'NA', 'NONE', 'NULL', '---', '--', '-', 'TBD', 'UNKNOWN',
})


def normalize_bank_coordinate(raw_value):
    """Strip placeholders; return None when the coordinate is unusable for EFT."""
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text or text.upper() in _PLACEHOLDER_BANK_COORDS:
        return None
    return text


def bank_coordinate_triplet(account_or_dict):
    """Return (institution, transit, account_number) normalized from a model or dict."""
    if account_or_dict is None:
        return None, None, None
    if isinstance(account_or_dict, dict):
        return (
            normalize_bank_coordinate(account_or_dict.get('institution_number')),
            normalize_bank_coordinate(account_or_dict.get('transit_number')),
            normalize_bank_coordinate(account_or_dict.get('account_number')),
        )
    return (
        normalize_bank_coordinate(getattr(account_or_dict, 'institution_number', None)),
        normalize_bank_coordinate(getattr(account_or_dict, 'transit_number', None)),
        normalize_bank_coordinate(getattr(account_or_dict, 'account_number', None)),
    )


def missing_bank_coordinate_labels(account_or_dict) -> list:
    institution, transit, account_number = bank_coordinate_triplet(account_or_dict)
    missing = []
    if not institution:
        missing.append('institution number')
    if not transit:
        missing.append('transit number')
    if not account_number:
        missing.append('account number')
    return missing


def bank_coordinates_complete(account_or_dict) -> bool:
    return not missing_bank_coordinate_labels(account_or_dict)


def incomplete_bank_coordinates_message(account_or_dict, *, role: str) -> str | None:
    """Human-readable funding blocker when institution/transit/account is incomplete."""
    missing = missing_bank_coordinate_labels(account_or_dict)
    if not missing:
        return None
    return (
        f'{role} is missing {", ".join(missing)}. '
        'Update banking from sync data or Edit bank details before funding.'
    )


def is_payment_blocked_institution(raw_value) -> bool:
    """True when the institution is in the agent-review risk set (621/623/703)."""
    return normalize_institution_number(raw_value) in PAYMENT_RISK_INSTITUTIONS


def payment_blocked_message(raw_value) -> str:
    return payment_risk_warning_message(raw_value)


def payment_risk_warning_message(raw_value) -> str:
    return PAYMENT_RISK_WARNING_MESSAGE.format(
        institution=normalize_institution_number(raw_value) or 'unknown'
    )
