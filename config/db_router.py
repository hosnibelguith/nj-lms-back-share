from django.conf import settings

from .tenant_context import get_current_tenant_database


class TenantDatabaseRouter:
    """
    Route tenant-owned data to the database chosen for the current request.

    Apps outside TENANT_DATABASE_APPS remain on default so infrastructure data
    can stay centralized when needed.
    """

    def _is_tenant_model(self, model) -> bool:
        return model._meta.app_label in settings.TENANT_DATABASE_APPS

    def db_for_read(self, model, **hints):
        if self._is_tenant_model(model):
            return get_current_tenant_database()
        return None

    def db_for_write(self, model, **hints):
        if self._is_tenant_model(model):
            return get_current_tenant_database()
        return None

    def allow_relation(self, obj1, obj2, **hints):
        tenant_apps = settings.TENANT_DATABASE_APPS
        if obj1._meta.app_label in tenant_apps or obj2._meta.app_label in tenant_apps:
            if obj1._state.db and obj2._state.db:
                return obj1._state.db == obj2._state.db
            return None
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in settings.TENANT_DATABASE_APPS:
            return db in settings.TENANT_DATABASE_ALIASES
        return db == "default"
