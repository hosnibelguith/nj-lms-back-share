from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from .models import PluginSettings

OWNER_EMAIL = 'mohamed+aclms@icollector.ai'


def is_owner(user):
    return bool(user and user.is_authenticated and user.is_active and user.is_staff
                and getattr(user, 'user_type', '') == 'staff'
                and user.email.strip().casefold() == OWNER_EMAIL)


def configuration():
    return PluginSettings.objects.using('default').filter(pk=1).first()


def require_enabled():
    config = configuration()
    if not config or not config.enabled:
        raise PermissionDenied('Payment processor is unplugged in Django admin.')
    return config


def writes_enabled():
    config = configuration()
    return bool(config and config.enabled and config.allow_provider_writes
                and getattr(settings, 'PAYMENT_PROCESSOR_ALLOW_WRITES', False)
                and getattr(settings, 'ZUMRAILS_TRANSACTIONS_ENABLED', True))


class OwnerOnly(BasePermission):
    def has_permission(self, request, view):
        if not is_owner(request.user):
            return False
        require_enabled()
        return True
