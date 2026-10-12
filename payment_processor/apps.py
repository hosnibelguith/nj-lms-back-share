from django.apps import AppConfig


class PaymentProcessorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payment_processor'
    verbose_name = 'Payment processor'
