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
                # Staff actions (approve/decline/fund) write a richer Activity History row.
                if getattr(instance, '_suppress_status_activity', False):
                    pass
                else:
                    from activity.models import ActivityHistory
                    from activity.services import actor_id, actor_label

                    activity_types = {
                        'ibv_pending': ('system', 'IBV Pending'),
                        'pending_signature': ('contract_sent', 'Pending Signature'),
                        'human_declined': ('system', 'Loan Declined'),
                        'expired': ('system', 'Application Expired'),
                        'pending_funding': ('system', 'Loan Approved'),
                        'active': ('loan_funded', 'Loan Funded'),
                        'paid_off': ('loan_paid_off', 'Loan Paid Off'),
                        'defaulted': ('loan_defaulted', 'Loan In Collections'),
                        'stopped': ('loan_defaulted', 'Loan Stopped'),
                    }

                    activity_type, title = activity_types.get(
                        instance.status,
                        ('system', 'Loan Status Changed')
                    )

                    actor_user = getattr(instance, 'approved_by', None)
                    created_by = actor_id(actor_user) if actor_user else 'system'
                    actor = actor_label(actor_user) if actor_user else 'System'
                    description = (
                        f'Status changed from {old_instance.get_status_display()} '
                        f'to {instance.get_status_display()} by {actor}.'
                    )

                    ActivityHistory.objects.create(
                        customer=instance.customer,
                        loan=instance,
                        type=activity_type,
                        title=title,
                        description=description,
                        created_by=created_by,
                        metadata={
                            'loan_id': str(instance.id),
                            'previous_status': old_instance.status,
                            'new_status': instance.status,
                        },
                    )

            if old_instance.ai_decision != instance.ai_decision and instance.ai_decision:
                from activity.models import ActivityHistory

                ActivityHistory.objects.create(
                    customer=instance.customer,
                    loan=instance,
                    type='system',
                    title='AI Decision',
                    description=f'AI Decision changed to {instance.get_ai_decision_display()}.',
                    created_by='system',
                    metadata={'loan_id': str(instance.id)},
                )
        except Loan.DoesNotExist:
            pass


@receiver(pre_save, sender=Payment)
def payment_status_changed(sender, instance, **kwargs):
    """Log payment status changes and system date adjustments."""
    if getattr(instance, '_suppress_payment_activity', False):
        return

    old_instance = None
    if instance.pk:
        try:
            old_instance = Payment.objects.get(pk=instance.pk)
        except Payment.DoesNotExist:
            old_instance = None

    if old_instance is None:
        original = getattr(instance, 'original_date', None)
        scheduled = getattr(instance, 'scheduled_date', None)
        if original and scheduled and original != scheduled:
            from activity.services import log_payment_date_adjustment

            log_payment_date_adjustment(payment=instance)
        return

    if old_instance.status != instance.status:
        from activity.models import ActivityHistory

        if instance.status == 'completed':
            ActivityHistory.objects.create(
                customer=instance.loan.customer,
                loan=instance.loan,
                type='payment_completed',
                title='Payment Received',
                description=f'Payment of ${instance.amount} received (status changed from {old_instance.status} to completed).',
                created_by='system',
                metadata={'loan_id': str(instance.loan_id)},
            )
        elif instance.status == 'failed':
            ActivityHistory.objects.create(
                customer=instance.loan.customer,
                loan=instance.loan,
                type='payment_failed',
                title='Payment Failed',
                description=f'Payment of ${instance.amount} failed: {instance.failure_reason or "Unknown reason"}',
                created_by='system',
                metadata={'loan_id': str(instance.loan_id)},
            )
        elif instance.status == 'nsf':
            ActivityHistory.objects.create(
                customer=instance.loan.customer,
                loan=instance.loan,
                type='payment_failed',
                title='Payment NSF',
                description=f'Payment of ${instance.amount} returned NSF',
                created_by='system',
                metadata={'loan_id': str(instance.loan_id)},
            )

    if old_instance.scheduled_date != instance.scheduled_date:
        from activity.services import log_payment_date_adjustment

        log_payment_date_adjustment(
            payment=instance,
            previous_date=old_instance.scheduled_date,
        )
