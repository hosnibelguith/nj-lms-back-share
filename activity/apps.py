# activity/apps.py
from django.apps import AppConfig


class ActivityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'activity'
    verbose_name = 'Activity & Audit Logs'

    def ready(self):
        # Import signals for automatic activity logging
        pass
