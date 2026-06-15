# contracts/apps.py
from django.apps import AppConfig


class ContractsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'contracts'
    verbose_name = 'Contracts & E-Signatures'

    def ready(self):
        # Import signals for contract status changes
        pass
