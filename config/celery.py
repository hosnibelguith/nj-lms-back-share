# config/celery.py
"""
Celery configuration for LendStack project.
"""
import os
from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('lendstack')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

# Celery Beat Schedule (periodic tasks)
app.conf.beat_schedule = {
    # Process scheduled payments daily at 6 AM
    'process-scheduled-payments': {
        'task': 'loans.tasks.process_scheduled_payments',
        'schedule': crontab(hour=6, minute=0),
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
    
    # Check for expired contracts daily at midnight
    'check-expired-contracts': {
        'task': 'contracts.tasks.check_expired_contracts',
        'schedule': crontab(hour=0, minute=0),
    },
    
    # Cleanup old activities monthly on the 1st at 2 AM
    'cleanup-old-activities': {
        'task': 'activity.tasks.cleanup_old_activities',
        'schedule': crontab(day_of_month=1, hour=2, minute=0),
        'kwargs': {'days': 365},
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
