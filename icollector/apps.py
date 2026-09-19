from django.apps import AppConfig


class ICollectorConfig(AppConfig):
    name = 'icollector'
    verbose_name = 'iCollector integration'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from . import signals  # noqa: F401
