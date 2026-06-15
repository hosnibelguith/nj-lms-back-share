# loans/tasks.py
"""
Simplified Loan Tasks - Only Loan and Payment models.
"""
from celery import shared_task
from django.utils import timezone
from datetime import date, timedelta
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
    
    if loan.status not in ['ai_approved', 'human_approved', 'pending']:
        logger.warning(f"Loan {loan_id} not in approved status")
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
    Process all scheduled payments due today.
    Run daily via celery beat.
    """
    from .models import Payment
    
    today = date.today()
    
    payments = Payment.objects.filter(
        scheduled_date__lte=today,
        status='scheduled'
    ).select_related('loan', 'loan__customer')
    
    logger.info(f"Processing {payments.count()} scheduled payments")
    
    processed = 0
    failed = 0
    
    for payment in payments:
        try:
            # Mark as pending
            payment.status = 'pending'
            payment.save()
            
            # TODO: Call payment processor API (EFT/PAD)
            # For now, simulate 95% success rate
            import random
            success = random.random() < 0.95
            
            if success:
                payment.complete()
                processed += 1
                logger.info(f"Payment {payment.id} completed")
            else:
                payment.fail("Payment declined by bank")
                failed += 1
                logger.warning(f"Payment {payment.id} failed")
                
        except Exception as e:
            logger.error(f"Error processing payment {payment.id}: {e}")
            payment.fail(str(e))
            failed += 1
    
    logger.info(f"Processed {processed} payments, {failed} failed")
    return {'processed': processed, 'failed': failed}


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
    for loan in active_loans:
        nsf_count = loan.payments.filter(status='nsf').count()
        
        if nsf_count >= 3:
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
    
    tomorrow = date.today() + timedelta(days=1)
    
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
