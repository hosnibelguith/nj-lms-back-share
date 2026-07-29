# loans/signals.py
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import Loan, Payment


@receiver(post_save, sender=Loan)
def loan_created(sender, instance, created, **kwargs):
    """Log activity when loan is created."""
    if created:
        from activity.models import ActivityHistory
        ActivityHistory.objects.create(
            customer=instance.customer,
            loan=instance,
            type='loan_created',
            title='Loan Application Created',
            description=f'{instance.get_type_display()} loan application for ${instance.principal}'
        )


@receiver(pre_save, sender=Loan)
def loan_status_changed(sender, instance, **kwargs):
    """Log activity when loan status changes."""
    if instance.pk:
        try:
            old_instance = Loan.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                from activity.models import ActivityHistory
                
                activity_types = {
                    'ibv_pending': ('system', 'IBV Pending'),
                    'pending_signature': ('contract_sent', 'Pending Signature'),
                    'human_declined': ('loan_created', 'Human Declined'),
                    'pending_funding': ('contract_signed', 'Pending Funding'),
                    'active': ('loan_funded', 'Loan Active'),
                    'paid_off': ('loan_paid_off', 'Loan Paid Off'),
                    'defaulted': ('loan_defaulted', 'Loan In Collections'),
                }
                
                activity_type, title = activity_types.get(
                    instance.status,
                    ('system', 'Loan Status Changed')
                )
                
                ActivityHistory.objects.create(
                    customer=instance.customer,
                    loan=instance,
                    type=activity_type,
                    title=title,
                    description=f'Status changed from {old_instance.get_status_display()} to {instance.get_status_display()}'
                )

            if old_instance.ai_decision != instance.ai_decision and instance.ai_decision:
                from activity.models import ActivityHistory

                ActivityHistory.objects.create(
                    customer=instance.customer,
                    loan=instance,
                    type='system',
                    title='AI Decision',
                    description=f'AI Decision: {instance.get_ai_decision_display()}',
                )
        except Loan.DoesNotExist:
            pass


@receiver(pre_save, sender=Payment)
def payment_status_changed(sender, instance, **kwargs):
    """Log activity when payment status changes to completed or failed."""
    if instance.pk:
        try:
            old_instance = Payment.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                from activity.models import ActivityHistory
                
                if instance.status == 'completed':
                    ActivityHistory.objects.create(
                        customer=instance.loan.customer,
                        loan=instance.loan,
                        type='payment_completed',
                        title='Payment Received',
                        description=f'Payment of ${instance.amount} received'
                    )
                elif instance.status == 'failed':
                    ActivityHistory.objects.create(
                        customer=instance.loan.customer,
                        loan=instance.loan,
                        type='payment_failed',
                        title='Payment Failed',
                        description=f'Payment of ${instance.amount} failed: {instance.failure_reason or "Unknown reason"}'
                    )
                elif instance.status == 'nsf':
                    ActivityHistory.objects.create(
                        customer=instance.loan.customer,
                        loan=instance.loan,
                        type='payment_failed',
                        title='Payment NSF',
                        description=f'Payment of ${instance.amount} returned NSF'
                    )
        except Payment.DoesNotExist:
            pass
