# activity/tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


@shared_task
def cleanup_old_activities(days: int = 365):
    """
    Clean up old activity entries to manage database size.
    Keeps activities for the specified number of days.
    
    Note: This should be run periodically (e.g., monthly) via celery beat.
    """
    from .models import ActivityHistory
    
    cutoff_date = timezone.now() - timedelta(days=days)
    
    # Don't delete critical activity types
    protected_types = [
        'loan_created', 'loan_funded', 'loan_paid_off', 'loan_defaulted',
        'contract_signed', 'customer_created', 'customer_blocked'
    ]
    
    deleted_count, _ = ActivityHistory.objects.filter(
        created_at__lt=cutoff_date
    ).exclude(
        type__in=protected_types
    ).delete()
    
    logger.info(f"Cleaned up {deleted_count} old activity entries")
    
    return f"Deleted {deleted_count} activities older than {days} days"


@shared_task
def log_system_activity(customer_id: str, activity_type: str, title: str, description: str, loan_id: str = None, metadata: dict = None):
    """
    Log a system-generated activity.
    Useful for logging events from background tasks.
    """
    from .models import ActivityHistory
    from accounts.models import Customer
    
    try:
        customer = Customer.objects.get(id=customer_id)
        
        ActivityHistory.objects.create(
            customer=customer,
            loan_id=loan_id,
            type=activity_type,
            title=title,
            description=description,
            metadata=metadata or {},
            created_by='system'
        )
        
        logger.info(f"Logged activity: {activity_type} for customer {customer_id}")
        
    except Customer.DoesNotExist:
        logger.error(f"Customer not found: {customer_id}")


@shared_task
def generate_activity_report(customer_id: str, start_date: str, end_date: str):
    """
    Generate an activity report for a customer.
    Returns a summary of all activities in the date range.
    """
    from .models import ActivityHistory
    from django.db.models import Count
    
    activities = ActivityHistory.objects.filter(
        customer_id=customer_id,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date
    )
    
    # Count by type
    type_counts = activities.values('type').annotate(count=Count('id'))
    
    report = {
        'customer_id': customer_id,
        'start_date': start_date,
        'end_date': end_date,
        'total_activities': activities.count(),
        'by_type': {item['type']: item['count'] for item in type_counts},
        'recent_activities': list(
            activities.order_by('-created_at').values(
                'id', 'type', 'title', 'description', 'created_at'
            )[:10]
        )
    }
    
    return report


@shared_task
def sync_activity_from_events(event_type: str, event_data: dict):
    """
    Create activity entries from external events/webhooks.
    Useful for integrating with external systems.
    """
    from .models import ActivityHistory
    from accounts.models import Customer
    
    customer_id = event_data.get('customer_id')
    if not customer_id:
        logger.warning(f"No customer_id in event data: {event_type}")
        return
    
    try:
        customer = Customer.objects.get(id=customer_id)
    except Customer.DoesNotExist:
        logger.error(f"Customer not found: {customer_id}")
        return
    
    # Map external events to activity types
    event_mapping = {
        'payment.completed': ('payment_completed', 'Payment Completed', 'External payment processed'),
        'payment.failed': ('payment_failed', 'Payment Failed', 'External payment failed'),
        'bank.connected': ('bank_connected', 'Bank Connected', 'Bank account connected via Flinks'),
        'ibv.completed': ('ibv_completed', 'IBV Completed', 'Instant bank verification completed'),
    }
    
    if event_type in event_mapping:
        activity_type, title, default_description = event_mapping[event_type]
        
        ActivityHistory.objects.create(
            customer=customer,
            loan_id=event_data.get('loan_id'),
            type=activity_type,
            title=title,
            description=event_data.get('description', default_description),
            metadata=event_data,
            created_by='system'
        )
        
        logger.info(f"Created activity from event: {event_type} for customer {customer_id}")
