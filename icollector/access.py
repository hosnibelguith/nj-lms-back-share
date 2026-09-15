from django.conf import settings
from rest_framework.permissions import BasePermission

from .models import PluginSettings


def configuration():
    return PluginSettings.objects.using('default').filter(pk=1).first()


def enabled():
    return PluginSettings.objects.using('default').filter(pk=1, enabled=True).exists()


def mohawk_staff(user):
    from config.tenant_context import get_current_tenant_database
    if get_current_tenant_database() != 'default':
        return False
    if not (user and user.is_authenticated and user.is_active
            and getattr(user, 'user_type', None) == 'staff'):
        return False
    # effective_lender can create the default lender; authorization must be read-only.
    from accounts.models import Lender
    lender_id = getattr(user, 'lender_id', None)
    lenders = Lender.objects.using('default').filter(slug='mohawkloans', is_active=True)
    return lenders.filter(pk=lender_id).exists() if lender_id else lenders.exists()


class MohawkStaff(BasePermission):
    def has_permission(self, request, view):
        return mohawk_staff(request.user)


def configured():
    return all(getattr(settings, key, '') for key in (
        'ICOLLECTOR_BASE_URL', 'ICOLLECTOR_API_KEY', 'ICOLLECTOR_HMAC_SECRET'))
