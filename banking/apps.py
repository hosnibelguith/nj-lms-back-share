# banking/apps.py
from django.apps import AppConfig


class BankingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'banking'
    verbose_name = 'Banking & IBV'

    def ready(self):
        # Import signals for webhook processing
        pass
