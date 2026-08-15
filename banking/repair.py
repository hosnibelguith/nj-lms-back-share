from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from accounts.models import Customer
from activity.services import log_staff_action
from banking.models import BankAccount, BankConnection, BankTransaction
from loans.models import Loan


REPAIRABLE_LOAN_STATUSES = ("ibv_pending", "expired")
BLOCKING_LOAN_STATUSES = (
    "ibv_pending",
    "pending",
    "pending_signature",
    "pending_funding",
    "active",
)


@dataclass(frozen=True)
class SyncedIBVRepairPlan:
    customer: Customer
    loan: Loan
    restore_connection: BankConnection
    deactivate_connections: tuple[BankConnection, ...]
    bank_account: BankAccount | None
    reason: str


@dataclass(frozen=True)
class SyncedIBVRepairResult:
    customer_id: str
    customer_email: str
    loan_id: str
    previous_loan_status: str
    new_loan_status: str
    restored_connection_id: str
    deactivated_connection_ids: tuple[str, ...]
    bank_account_id: str | None


def connection_has_synced_ibv_data(
    connection: BankConnection,
    *,
    require_transactions: bool = True,
) -> bool:
    if connection.sync_status != "synced":
        return False
    if not BankAccount.objects.filter(connection=connection).exists():
        return False
    if require_transactions and not BankTransaction.objects.filter(
        account__connection=connection
    ).exists():
        return False
    return True


def find_repairable_synced_ibv(
    customer: Customer,
    *,
    login_id: str | None = None,
    statuses: Iterable[str] = REPAIRABLE_LOAN_STATUSES,
    require_transactions: bool = True,
) -> SyncedIBVRepairPlan | None:
    loan = (
        customer.loans.filter(status__in=tuple(statuses))
        .order_by("-created_at")
        .first()
    )
    if loan is None:
        return None

    if loan.status == "expired":
        conflicting = (
            customer.loans.filter(status__in=BLOCKING_LOAN_STATUSES)
            .exclude(id=loan.id)
            .exists()
        )
        if conflicting:
            return None

    active_connections = tuple(
        BankConnection.objects.filter(customer=customer, is_active=True).order_by(
            "-created_at"
        )
    )
    active_login_ids = [c.login_id for c in active_connections if c.login_id]

    candidates = BankConnection.objects.filter(customer=customer)
    if login_id:
        candidates = candidates.filter(login_id=str(login_id))
    elif active_login_ids:
        candidates = candidates.filter(login_id__in=active_login_ids)

    synced_candidates = [
        connection
        for connection in candidates.order_by("-last_synced_at", "-updated_at", "-created_at")
        if connection_has_synced_ibv_data(
            connection, require_transactions=require_transactions
        )
    ]
    if not synced_candidates:
        return None

    restore_connection = synced_candidates[0]
    deactivate_connections = tuple(
        connection
        for connection in active_connections
        if connection.id != restore_connection.id
    )
    bank_account = _preferred_bank_account(restore_connection)
    reason = (
        "Existing synced IBV data found on an inactive connection."
        if not restore_connection.is_active
        else "Existing synced IBV data found on the active connection."
    )
    return SyncedIBVRepairPlan(
        customer=customer,
        loan=loan,
        restore_connection=restore_connection,
        deactivate_connections=deactivate_connections,
        bank_account=bank_account,
        reason=reason,
    )


@transaction.atomic
def apply_synced_ibv_repair(plan: SyncedIBVRepairPlan) -> SyncedIBVRepairResult:
    customer = Customer.objects.select_for_update().get(id=plan.customer.id)
    loan = Loan.objects.select_for_update().get(id=plan.loan.id)
    restore_connection = BankConnection.objects.select_for_update().get(
        id=plan.restore_connection.id
    )

    previous_status = loan.status
    now = timezone.now()

    BankConnection.objects.filter(
        id__in=[connection.id for connection in plan.deactivate_connections]
    ).exclude(id=restore_connection.id).update(is_active=False)

    restore_connection.is_active = True
    restore_connection.sync_status = "synced"
    restore_connection.sync_error = None
    if restore_connection.last_synced_at is None:
        restore_connection.last_synced_at = now
    restore_connection.save(
        update_fields=[
            "is_active",
            "sync_status",
            "sync_error",
            "last_synced_at",
            "updated_at",
        ]
    )

    customer.banking_verified = True
    customer.onboarding_stage = "contract"
    customer.save(update_fields=["banking_verified", "onboarding_stage", "updated_at"])

    bank_account = _preferred_bank_account(restore_connection)
    if bank_account is not None:
        loan.bank_account = bank_account
        if loan.collections_account_id is None:
            loan.collections_account = bank_account

    contract_id = loan.contract_id or f"demo-contract-{str(loan.id)[:8]}"
    loan.mark_contract_sent(contract_id=contract_id)

    if bank_account is not None:
        update_fields = ["bank_account", "updated_at"]
        if loan.collections_account_id == bank_account.id:
            update_fields.append("collections_account")
        loan.save(update_fields=update_fields)

    deactivated_ids = tuple(str(connection.id) for connection in plan.deactivate_connections)
    log_staff_action(
        customer=customer,
        loan=loan,
        user=None,
        type_value="system",
        title="IBV Data Restored",
        description=(
            "Existing synced IBV data was restored and the loan was moved "
            "to waiting for client signature."
        ),
        metadata={
            "source": "synced_ibv_repair",
            "previous_status": previous_status,
            "new_status": loan.status,
            "restored_connection_id": str(restore_connection.id),
            "deactivated_connection_ids": list(deactivated_ids),
            "bank_account_id": str(bank_account.id) if bank_account else None,
        },
    )

    return SyncedIBVRepairResult(
        customer_id=str(customer.id),
        customer_email=customer.email,
        loan_id=str(loan.id),
        previous_loan_status=previous_status,
        new_loan_status=loan.status,
        restored_connection_id=str(restore_connection.id),
        deactivated_connection_ids=deactivated_ids,
        bank_account_id=str(bank_account.id) if bank_account else None,
    )


def _preferred_bank_account(connection: BankConnection) -> BankAccount | None:
    return (
        BankAccount.objects.filter(connection=connection)
        .order_by("-is_primary", "-use_for_eft_funding", "-use_for_eft_collections", "name")
        .first()
    )
