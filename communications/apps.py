# communications/apps.py
from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'communications'
    verbose_name = 'Communications (Email/SMS)'

    def ready(self):
        # Import signals for communication tracking
        pass
