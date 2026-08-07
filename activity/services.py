"""Staff-facing activity / audit trail helpers."""
from __future__ import annotations

from django.utils import timezone

from activity.models import ActivityHistory

# Staff bell / funding-alerts feed.
FUNDING_ALERT_TITLES = (
    "Funding Failed",
    "Funding Returned",
    "Funding Cancelled",
)


def resolve_funding_failure_alerts(loan, *, reason: str = "resolved") -> int:
    """Mark unresolved funding failure alerts for a loan so they leave the bell.

    Called when a new funding attempt is successfully initiated or funding completes.
    Uses metadata.is_resolved (no schema migration).
    """
    if loan is None or getattr(loan, "id", None) is None:
        return 0

    qs = ActivityHistory.objects.filter(
        loan_id=loan.id,
        title__in=FUNDING_ALERT_TITLES,
    )
    updated = 0
    now_iso = timezone.now().isoformat()
    for activity in qs.iterator():
        meta = dict(activity.metadata or {})
        if meta.get("is_resolved") is True:
            continue
        meta["is_resolved"] = True
        meta["resolved_at"] = now_iso
        meta["resolved_reason"] = reason
        activity.metadata = meta
        activity.save(update_fields=["metadata"])
        updated += 1
    return updated


def actor_label(user) -> str:
    """Human-readable name for activity descriptions."""
    if user is None:
        return "System"
    if isinstance(user, str):
        return user
    name = (
        getattr(user, "full_name", None)
        or getattr(user, "get_full_name", lambda: None)()
        or getattr(user, "email", None)
    )
    return (name or "Staff").strip() or "Staff"


def actor_id(user) -> str:
    if user is None:
        return "system"
    if isinstance(user, str):
        return user
    return str(getattr(user, "id", "staff"))


def format_account_label(account_or_snapshot) -> str:
    """Short bank account label for before→after logs."""
    if not account_or_snapshot:
        return "(none)"

    if hasattr(account_or_snapshot, "institution_number"):
        inst = getattr(account_or_snapshot, "institution_number", "") or ""
        transit = getattr(account_or_snapshot, "transit_number", "") or ""
        number = getattr(account_or_snapshot, "account_number", "") or ""
        name = getattr(account_or_snapshot, "name", "") or ""
    elif isinstance(account_or_snapshot, dict):
        inst = account_or_snapshot.get("institution_number") or ""
        transit = account_or_snapshot.get("transit_number") or ""
        number = account_or_snapshot.get("account_number") or ""
        name = account_or_snapshot.get("name") or ""
        if not number and account_or_snapshot.get("account_last4"):
            number = f"****{account_or_snapshot.get('account_last4')}"
    else:
        return str(account_or_snapshot)

    last4 = number[-4:] if number else ""
    bits = [p for p in (name, f"Inst {inst}" if inst else "", f"Transit {transit}" if transit else "", f"****{last4}" if last4 else "") if p]
    return " · ".join(bits) if bits else "(none)"


def log_staff_action(
    *,
    customer,
    loan=None,
    user=None,
    type_value: str = "system",
    title: str,
    description: str,
    metadata: dict | None = None,
) -> ActivityHistory | None:
    """
    Write one Activity History row for a staff (or system) action.

    Prefer descriptions like:
      "Approved by Jane Doe. Status changed from Pending to Pending Funding."
      "Approved amount changed from $500.00 to $400.00 by Jane Doe."
    """
    try:
        meta = dict(metadata or {})
        if loan is not None:
            meta.setdefault("loan_id", str(loan.id))
        if user is not None and "actor" not in meta:
            meta["actor"] = actor_label(user)

        return ActivityHistory.objects.create(
            customer=customer,
            loan=loan,
            type=type_value,
            title=title,
            description=description,
            created_by=actor_id(user),
            metadata=meta,
        )
    except Exception:
        # Audit logging must never break the primary action.
        import logging

        logging.getLogger(__name__).exception(
            "Failed to log staff action title=%s customer=%s",
            title,
            getattr(customer, "id", None),
        )
        return None
