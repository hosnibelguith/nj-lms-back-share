"""Same-customer / same-day inbound email grouping for inbox counts and replies."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from communications.models import Communication


def _contact_filter(communication: Communication) -> Q:
    if communication.customer_id:
        return Q(customer_id=communication.customer_id)
    address = (communication.from_address or "").strip().lower()
    if address:
        return Q(customer__isnull=True, from_address__iexact=address)
    return Q(pk=communication.pk)


def local_day_bounds(moment: datetime | None = None) -> tuple[datetime, datetime]:
    """Inclusive start / exclusive end of the local calendar day for ``moment``."""
    when = timezone.localtime(moment) if moment else timezone.localtime()
    start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def same_day_inbound_email_queryset(
    communication: Communication,
    *,
    unanswered_only: bool = False,
) -> QuerySet:
    """Inbound emails for the same customer/contact on the same local day."""
    if communication.type != "email" or communication.direction != "inbound":
        return Communication.objects.none()

    start, end = local_day_bounds(communication.created_at)
    qs = Communication.objects.filter(
        type="email",
        direction="inbound",
        created_at__gte=start,
        created_at__lt=end,
    ).filter(_contact_filter(communication))
    if unanswered_only:
        qs = qs.filter(
            is_answered=False,
            incoming_status__in=["new", "unanswered"],
        )
    return qs


@transaction.atomic
def mark_same_day_inbound_answered(
    communication: Communication,
    *,
    opened_by: str | None = None,
    answered_at=None,
) -> int:
    """Mark the target + same-day unanswered siblings as answered. Returns rows updated."""
    answered_at = answered_at or timezone.now()
    sibling_ids = list(
        same_day_inbound_email_queryset(
            communication, unanswered_only=True
        ).values_list("pk", flat=True)
    )
    ids = set(sibling_ids)
    ids.add(communication.pk)

    updated = Communication.objects.filter(pk__in=ids, is_answered=False).update(
        is_answered=True,
        incoming_status="read",
        answered_at=answered_at,
    )
    Communication.objects.filter(pk__in=ids, opened_at__isnull=True).update(
        opened_at=answered_at,
        opened_by=opened_by,
    )
    return updated


def group_key_for_inbound(communication: Communication) -> tuple:
    day = timezone.localdate(communication.created_at)
    if communication.customer_id:
        return ("customer", str(communication.customer_id), day.isoformat())
    address = (communication.from_address or "").strip().lower()
    return ("address", address, day.isoformat())


def collapsed_unanswered_email_counts(queryset: QuerySet) -> dict:
    """Count unanswered inbound emails once per customer/contact per local day.

    Representative status is taken from the newest email in each group.
    """
    inbound_unanswered = (
        queryset.filter(
            type="email",
            direction="inbound",
            is_answered=False,
            incoming_status__in=["new", "unanswered"],
        )
        .select_related(None)
        .order_by("-created_at")
        .only("id", "customer_id", "from_address", "incoming_status", "created_at")
    )

    groups: dict[tuple, str] = {}
    for row in inbound_unanswered.iterator(chunk_size=500):
        key = group_key_for_inbound(row)
        if key not in groups:
            groups[key] = row.incoming_status

    new_count = sum(1 for status in groups.values() if status == "new")
    opened_unanswered_count = sum(
        1 for status in groups.values() if status == "unanswered"
    )
    return {
        "unanswered_count": new_count + opened_unanswered_count,
        "new_count": new_count,
        "opened_unanswered_count": opened_unanswered_count,
    }
