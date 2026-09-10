from django.test import RequestFactory, SimpleTestCase, override_settings

from config.db_router import TenantDatabaseRouter
from config.tenant_context import get_current_tenant_database, set_current_tenant_database
from config.tenant_middleware import TenantDatabaseMiddleware


class _TenantModel:
    class _meta:
        app_label = "accounts"


class _GlobalModel:
    class _meta:
        app_label = "staticfiles"


class TenantDatabaseRoutingTests(SimpleTestCase):
    def tearDown(self):
        set_current_tenant_database("default")

    @override_settings(
        TENANT_DATABASE_ALIASES={"default", "novaloans"},
        TENANT_DEFAULT_DATABASE_ALIAS="default",
    )
    def test_context_rejects_unknown_alias(self):
        set_current_tenant_database("missing")

        self.assertEqual(get_current_tenant_database(), "default")

    @override_settings(
        TENANT_DATABASE_APPS={"accounts"},
        TENANT_DATABASE_ALIASES={"default", "novaloans"},
        TENANT_DEFAULT_DATABASE_ALIAS="default",
    )
    def test_router_uses_current_alias_for_tenant_models(self):
        set_current_tenant_database("novaloans")
        router = TenantDatabaseRouter()

        self.assertEqual(router.db_for_read(_TenantModel), "novaloans")
        self.assertEqual(router.db_for_write(_TenantModel), "novaloans")
        self.assertIsNone(router.db_for_read(_GlobalModel))

    @override_settings(
        ALLOWED_HOSTS=["app.novaloans.com"],
        DEBUG=False,
        TENANT_DATABASE_DOMAIN_MAP={"app.novaloans.com": "novaloans"},
        TENANT_DATABASE_ALIASES={"default", "novaloans"},
        TENANT_DEFAULT_DATABASE_ALIAS="default",
        TENANT_DEBUG_HEADER_ENABLED=False,
    )
    def test_middleware_selects_database_from_host(self):
        request = RequestFactory().get("/", HTTP_HOST="app.novaloans.com")
        middleware = TenantDatabaseMiddleware(lambda req: req.tenant_database_alias)

        self.assertEqual(middleware(request), "novaloans")
