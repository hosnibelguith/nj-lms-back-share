from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import Customer


@receiver(post_save, sender=Customer)
def customer_created(sender, instance, created, **kwargs):
    """Log activity when customer is created."""
    if created:
        try:
            from activity.models import ActivityHistory
            ActivityHistory.objects.create(
                customer=instance,
                type='customer_created',
                title='Customer Created',
                description=f'Customer {instance.full_name} was created'
            )
        except Exception:
            pass


@receiver(pre_save, sender=Customer)
def customer_status_changed(sender, instance, **kwargs):
    """Log activity when customer status changes."""
    if instance.pk:
        try:
            old_instance = Customer.objects.get(pk=instance.pk)
            if old_instance.status != instance.status:
                try:
                    from activity.models import ActivityHistory
                    ActivityHistory.objects.create(
                        customer=instance,
                        type='status_changed',
                        title='Customer Status Changed',
                        description=f'Status changed from {old_instance.status} to {instance.status}'
                    )
                except Exception:
                    pass
        except Customer.DoesNotExist:
            pass