# loans/tasks.py
"""
Simplified Loan Tasks - Only Loan and Payment models.
"""
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging
import uuid

logger = logging.getLogger(__name__)


@shared_task
def send_contract_task(loan_id: str):
    """Send contract via DocuSign."""
    from .models import Loan
    
    try:
        loan = Loan.objects.get(id=loan_id)
    except Loan.DoesNotExist:
        logger.error(f"Loan {loan_id} not found")
        return False
    
    if loan.status not in ['ibv_pending', 'pending']:
        logger.warning(f"Loan {loan_id} not pending signature request")
        return False
    
    # TODO: Integrate with DocuSign API
    # 1. Generate contract PDF
    # 2. Send to DocuSign
    # 3. Get envelope_id
    
    # For now, placeholder
    contract_id = f"docusign_{uuid.uuid4().hex[:12]}"
    loan.mark_contract_sent(contract_id)
    
    logger.info(f"Contract sent for loan {loan_id}, contract_id={contract_id}")
    return True


@shared_task
def process_scheduled_payments():
    """
    Initiate Zūm EFT collections for payments whose send window has opened.

    Instructions are sent after 7:00 PM America/Toronto on the calendar day
    before the adjusted payment date. Overdue rows remain eligible as catch-up.
    """
    from .business_calendar import is_instruction_send_ready, local_today
    from .models import Payment
    from .zumrails import CollectionService

    today = local_today()
    latest_due = today + timedelta(days=1)

    payments = Payment.objects.filter(
        scheduled_date__lte=latest_due,
        status='scheduled',
        loan__status='active',
    ).select_related(
        'loan',
        'loan__customer',
        'loan__collections_account',
        'loan__bank_account',
    )

    logger.info("Queuing %s scheduled payments for Zūm collection", payments.count())

    initiated = 0
    skipped = 0
    errors = 0

    for payment in payments:
        if not is_instruction_send_ready(payment.scheduled_date):
            skipped += 1
            continue
        if payment.collection_attempts.filter(status__in=['processing', 'completed']).exists():
            skipped += 1
            continue

        try:
            CollectionService.initiate(
                loan=payment.loan,
                amount=payment.amount,
                payment=payment,
                user=None,
            )
            payment.status = 'pending'
            payment.save(update_fields=['status'])
            initiated += 1
        except Exception as exc:
            logger.error("Error initiating collection for payment %s: %s", payment.id, exc)
            errors += 1

    logger.info(
        "Scheduled collections: initiated=%s skipped=%s errors=%s",
        initiated,
        skipped,
        errors,
    )
    return {'initiated': initiated, 'skipped': skipped, 'errors': errors}


@shared_task
def check_defaulted_loans():
    """
    Check for loans that should be marked as defaulted.
    A loan is defaulted after 3+ NSF payments.
    Run daily via celery beat.
    """
    from .models import Loan
    
    active_loans = Loan.objects.filter(status='active')
    
    defaulted_count = 0
    from .collection_policy import AUTO_STOP_MODE_AFTER_MISSED, auto_stop_missed_count, auto_stop_mode, should_auto_stop_loan

    if auto_stop_mode() != AUTO_STOP_MODE_AFTER_MISSED:
        logger.info("Defaulted 0 loans (collection stop mode is manual)")
        return 0

    threshold = auto_stop_missed_count()
    for loan in active_loans:
        nsf_count = loan.payments.filter(status='nsf').count()
        if nsf_count >= threshold or should_auto_stop_loan(loan, ''):
            loan.mark_defaulted()
            defaulted_count += 1
            logger.warning(f"Loan {loan.id} defaulted ({nsf_count} NSFs)")
    
    logger.info(f"Defaulted {defaulted_count} loans")
    return defaulted_count


@shared_task
def send_payment_reminders():
    """
    Send payment reminders for payments due tomorrow.
    Run daily via celery beat.
    """
    from .models import Payment
    
    tomorrow = timezone.localdate() + timedelta(days=1)
    
    payments = Payment.objects.filter(
        scheduled_date=tomorrow,
        status='scheduled'
    ).select_related('loan', 'loan__customer')
    
    sent = 0
    for payment in payments:
        customer = payment.loan.customer
        
        # TODO: Send SMS/Email reminder
        # from communications.tasks import send_sms
        # send_sms.delay(str(customer.id), f"Payment reminder: ${payment.amount} due tomorrow")
        
        logger.info(f"Reminder sent to {customer.email} for ${payment.amount}")
        sent += 1
    
    logger.info(f"Sent {sent} payment reminders")
    return sent


@shared_task
def create_payment_schedule(loan_id: str, schedule: list):
    """
    Create payment schedule for a loan.
    
    schedule = [
        {'date': '2024-01-15', 'amount': 100.00},
        {'date': '2024-01-30', 'amount': 100.00},
        ...
    ]
    """
    from .models import Loan, Payment
    
    try:
        loan = Loan.objects.get(id=loan_id)
    except Loan.DoesNotExist:
        logger.error(f"Loan {loan_id} not found")
        return False
    
    for item in schedule:
        Payment.objects.create(
            loan=loan,
            amount=item['amount'],
            scheduled_date=item['date'],
            type='scheduled',
            status='scheduled'
        )
    
    logger.info(f"Created {len(schedule)} payments for loan {loan_id}")
    return True


@shared_task
def process_collection_settlements():
    from .zumrails import SettlementService

    completed = SettlementService.process_due()
    logger.info("Completed %s collection settlements", completed)
    return {"completed": completed}
