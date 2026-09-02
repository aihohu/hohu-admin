from functools import partial
from typing import Any

from app.core.tenant import TenantContext
from app.modules.marketplace.service.app_service import app_service as _app_service
from app.modules.marketplace.service.install_service import (
    install_service as _install_service,
)
from app.modules.marketplace.service.permission_service import (
    permission_service as _permission_service,
)
from app.modules.marketplace.service.rating_service import (
    rating_service as _rating_service,
)
from app.modules.marketplace.service.upload_service import (
    upload_service as _upload_service,
)
from app.modules.marketplace.service.version_service import (
    version_service as _version_service,
)

DEFAULT_TENANT = TenantContext(
    tenant_id=0,
    tenant_code="default",
    actor_user_id=1,
    tenant_version=1,
    source="access_token",
)


class _DefaultTenantService:
    """Test adapter that makes the single-mode authority explicit."""

    def __init__(self, service: Any, tenant_methods: set[str]) -> None:
        self._service = service
        self._tenant_methods = tenant_methods

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._service, name)
        if name in self._tenant_methods:
            return partial(value, tenant=DEFAULT_TENANT)
        return value


default_app_service = _DefaultTenantService(
    _app_service,
    {"create", "get_by_id", "get_by_slug", "list", "search"},
)
default_install_service = _DefaultTenantService(
    _install_service,
    {"disable", "enable", "install", "list_installed", "uninstall"},
)
default_permission_service = _DefaultTenantService(
    _permission_service,
    {"bulk_insert", "list_by_app"},
)
default_rating_service = _DefaultTenantService(
    _rating_service,
    {"create", "delete", "update"},
)
default_upload_service = _DefaultTenantService(_upload_service, {"save"})
default_version_service = _DefaultTenantService(
    _version_service,
    {"create", "get_by_version", "get_latest_approved"},
)
