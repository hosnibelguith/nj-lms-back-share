from django.conf import settings

from .tenant_context import reset_current_tenant_database, set_current_tenant_database


class TenantDatabaseMiddleware:
    """
    Select the request's database alias before authentication runs.

    Production should route by host/domain. In local DEBUG only, an optional
    header can be enabled to exercise non-default tenant databases from
    localhost without spoofing DNS.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        alias = self._alias_for_request(request)
        token = set_current_tenant_database(alias)
        request.tenant_database_alias = alias
        try:
            return self.get_response(request)
        finally:
            reset_current_tenant_database(token)

    def _alias_for_request(self, request) -> str:
        default_alias = getattr(settings, "TENANT_DEFAULT_DATABASE_ALIAS", "default")

        if settings.DEBUG and getattr(settings, "TENANT_DEBUG_HEADER_ENABLED", False):
            header_alias = request.headers.get("X-Tenant-Database", "").strip()
            if header_alias in settings.TENANT_DATABASE_ALIASES:
                return header_alias

        host = request.get_host().split(":", 1)[0].lower()
        return settings.TENANT_DATABASE_DOMAIN_MAP.get(host, default_alias)
