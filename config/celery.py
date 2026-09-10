# config/celery.py
"""
Celery configuration for LendStack project.
"""
import os
from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun
from django.conf import settings

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('lendstack')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


@task_prerun.connect
def set_task_tenant_database(sender=None, task=None, kwargs=None, **_):
    from config.tenant_context import set_current_tenant_database

    alias = (kwargs or {}).get("tenant_database_alias")
    token = set_current_tenant_database(alias)
    if task is not None:
        task._tenant_database_token = token


@task_postrun.connect
def reset_task_tenant_database(sender=None, task=None, **_):
    from config.tenant_context import reset_current_tenant_database

    token = getattr(task, "_tenant_database_token", None)
    if token is not None:
        reset_current_tenant_database(token)
        task._tenant_database_token = None

# Celery Beat Schedule (periodic tasks)
app.conf.beat_schedule = {
    # Process scheduled payments daily after 7:00 PM America/Toronto
    # (7:01 PM). Instructions go out the calendar day before the adjusted date.
    'process-scheduled-payments': {
        'task': 'loans.tasks.process_scheduled_payments',
        'schedule': crontab(hour=19, minute=1),
    },
    
    # Send payment reminders daily at 9 AM
    'send-payment-reminders': {
        'task': 'loans.tasks.send_payment_reminders',
        'schedule': crontab(hour=9, minute=0),
    },
    
    # Check for defaulted loans daily at 10 AM
    'check-defaulted-loans': {
        'task': 'loans.tasks.check_defaulted_loans',
        'schedule': crontab(hour=10, minute=0),
    },

    # Complete due Zūm collection settlements daily at 12 PM
    'process-collection-settlements': {
        'task': 'loans.tasks.process_collection_settlements',
        'schedule': crontab(hour=12, minute=0),
    },

    # Poll inbound customer emails every 5 minutes when enabled
    'poll-inbound-email': {
        'task': 'communications.tasks.poll_inbound_email',
        'schedule': crontab(minute='*/5'),
    },

    # Automated GAD restart for pending IBV that still has a LoginId.
    # Runs the same pull as staff Re-pull IBV, inline so Redis cannot drop it.
    'repull-recent-unsynced-ibv': {
        'task': 'banking.tasks.repull_recent_unsynced_ibv',
        'schedule': crontab(minute='*/5'),
    },
    
    # Check for expired contracts daily at midnight
    'check-expired-contracts': {
        'task': 'contracts.tasks.check_expired_contracts',
        'schedule': crontab(hour=0, minute=0),
    },

    # Offer early renewal to eligible collecting clients daily at 11 AM
    'send-early-renewal-offers': {
        'task': 'loans.tasks.send_early_renewal_offers',
        'schedule': crontab(hour=11, minute=0),
    },
    
    # Cleanup old activities monthly on the 1st at 2 AM
    'cleanup-old-activities': {
        'task': 'activity.tasks.cleanup_old_activities',
        'schedule': crontab(day_of_month=1, hour=2, minute=0),
        'kwargs': {'days': 365},
    },
}

for alias in sorted(settings.TENANT_DATABASE_ALIASES - {"default"}):
    for name, entry in list(app.conf.beat_schedule.items()):
        tenant_entry = dict(entry)
        tenant_kwargs = dict(tenant_entry.get("kwargs", {}))
        tenant_kwargs["tenant_database_alias"] = alias
        tenant_entry["kwargs"] = tenant_kwargs
        app.conf.beat_schedule[f"{name}-{alias}"] = tenant_entry


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
