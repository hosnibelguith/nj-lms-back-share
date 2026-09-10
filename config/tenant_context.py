from contextvars import ContextVar
from contextlib import contextmanager

from django.conf import settings


_current_tenant_database = ContextVar("current_tenant_database", default=None)


def get_current_tenant_database() -> str:
    alias = _current_tenant_database.get()
    if alias in getattr(settings, "TENANT_DATABASE_ALIASES", {"default"}):
        return alias
    return getattr(settings, "TENANT_DEFAULT_DATABASE_ALIAS", "default")


def set_current_tenant_database(alias: str | None):
    aliases = getattr(settings, "TENANT_DATABASE_ALIASES", {"default"})
    if alias not in aliases:
        alias = getattr(settings, "TENANT_DEFAULT_DATABASE_ALIAS", "default")
    return _current_tenant_database.set(alias)


def reset_current_tenant_database(token) -> None:
    _current_tenant_database.reset(token)


@contextmanager
def use_tenant_database(alias: str | None):
    token = set_current_tenant_database(alias)
    try:
        yield get_current_tenant_database()
    finally:
        reset_current_tenant_database(token)
