"""Staff-requested IBV email and SyncData received mark.

The SyncData Flinks iframe is not tied to this LMS customer, so completing
IBV there cannot be ingested automatically. Request IBV therefore emails a
link to this portal's banking page (existing Flinks receive path) and opens a
portal refill so the client can connect again. When an agent already confirmed
the file in SyncData, they mark it received so the client can continue without
bank rows in this LMS.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction

from accounts.models import Customer


IBV_REQUEST_TEMPLATE_NAME = 'Complete IBV Request'
# Unverify only while the file is still waiting on IBV or signature. Funded /
# collecting loans stay verified so the portal loan view is unchanged.
PRE_SIGNATURE_LOAN_STATUSES = ('ibv_pending', 'pending_signature')


def ibv_portal_url() -> str:
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    return f'{frontend_url}/customer/banking'


def clear_ibv_refill_requested(customer: Customer) -> bool:
    """Clear the staff refill flag. Returns True if a save ran."""
    if not customer.ibv_refill_requested:
        return False
    customer.ibv_refill_requested = False
    customer.save(update_fields=['ibv_refill_requested', 'updated_at'])
    return True


def _open_ibv_refill(customer: Customer) -> dict:
    from banking.models import BankConnection

    loan = customer.loans.order_by('-created_at').first()
    reopen_onboarding = loan is None or loan.status in PRE_SIGNATURE_LOAN_STATUSES

    BankConnection.objects.filter(
        customer=customer,
        provider='flinks',
        is_active=True,
    ).update(
        is_active=False,
        sync_status='failed',
        sync_error='Staff requested a new IBV in the portal.',
    )

    update_fields = ['ibv_refill_requested', 'updated_at']
    customer.ibv_refill_requested = True
    if reopen_onboarding:
        customer.banking_verified = False
        customer.ibv_source = ''
        update_fields.extend(['banking_verified', 'ibv_source'])
        if customer.onboarding_stage == 'contract':
            customer.onboarding_stage = 'banking_verification'
            update_fields.append('onboarding_stage')
    customer.save(update_fields=update_fields)
    return {
        'ibv_refill_requested': True,
        'banking_verified': customer.banking_verified,
        'onboarding_reopened': reopen_onboarding,
    }


def request_ibv(customer: Customer, *, user=None) -> dict:
    from activity.services import log_staff_action
    from communications.models import CommunicationTemplate
    from communications.tasks import send_template_message

    if not (customer.email or '').strip():
        raise ValueError('This customer has no email address.')

    template = CommunicationTemplate.objects.filter(
        name=IBV_REQUEST_TEMPLATE_NAME,
        type='email',
        is_active=True,
    ).first()
    if template is None:
        raise ValueError(
            f'Email template "{IBV_REQUEST_TEMPLATE_NAME}" is missing or inactive.'
        )

    loan = customer.loans.order_by('-created_at').first()
    refill = _open_ibv_refill(customer)
    url = ibv_portal_url()
    send_template_message.delay(
        str(customer.id),
        str(template.id),
        str(loan.id) if loan else None,
        extra_context={
            'ibv_url': url,
            'portal_url': url,
        },
    )
    log_staff_action(
        customer=customer,
        loan=loan,
        user=user,
        type_value='email_sent',
        title='IBV Requested',
        description=(
            'Staff sent the IBV request email and opened a portal refill. '
            'The client completes banking at /customer/banking so this LMS '
            'can receive the Flinks result.'
        ),
        metadata={
            'action': 'request_ibv',
            'ibv_url': url,
            'ibv_refill_requested': True,
            'onboarding_reopened': refill['onboarding_reopened'],
        },
    )
    return {
        'message': (
            'IBV request email queued. The client can complete a new IBV in the portal.'
        ),
        'ibv_url': url,
        'template_name': template.name,
        'ibv_refill_requested': True,
        'banking_verified': refill['banking_verified'],
    }


@transaction.atomic
def mark_ibv_received_syncdata(customer: Customer, *, user=None) -> dict:
    from activity.services import log_staff_action
    from loans.services import LoanService

    already = (
        customer.banking_verified
        and customer.ibv_source == Customer.IBV_SOURCE_SYNCDATA
    )
    if already:
        clear_ibv_refill_requested(customer)
        return {
            'message': 'IBV was already marked received in SyncData.',
            'banking_verified': True,
            'ibv_source': Customer.IBV_SOURCE_SYNCDATA,
            'already_marked': True,
        }

    has_accounts = customer.bank_accounts.exists()
    if (
        customer.banking_verified
        and has_accounts
        and customer.ibv_source != Customer.IBV_SOURCE_SYNCDATA
    ):
        raise ValueError(
            'IBV is already completed in this system. Check the IBV tab for accounts.'
        )

    customer.banking_verified = True
    customer.ibv_source = Customer.IBV_SOURCE_SYNCDATA
    customer.ibv_refill_requested = False
    if customer.onboarding_stage == 'banking_verification':
        customer.onboarding_stage = 'contract'
    customer.save(
        update_fields=[
            'banking_verified',
            'ibv_source',
            'ibv_refill_requested',
            'onboarding_stage',
            'updated_at',
        ]
    )

    advanced = 0
    for loan in customer.loans.filter(status='ibv_pending'):
        LoanService.mark_pending_signature(loan)
        advanced += 1

    log_staff_action(
        customer=customer,
        user=user,
        type_value='ibv_completed',
        title='IBV marked received (SyncData)',
        description=(
            'Staff marked IBV as received in SyncData. The client can continue '
            'to the next step. Bank details are not stored here — check SyncData, '
            'or add a void-cheque account before funding.'
        ),
        metadata={'action': 'mark_ibv_received_syncdata', 'loans_advanced': advanced},
    )
    return {
        'message': (
            'IBV saved in SyncData, please check there. The client can continue '
            'to the next step.'
        ),
        'banking_verified': True,
        'ibv_source': Customer.IBV_SOURCE_SYNCDATA,
        'already_marked': False,
        'loans_advanced': advanced,
    }
