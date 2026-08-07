from types import SimpleNamespace

from app.core.tenant import DEFAULT_TENANT_ID, resolve_tenant_id


def test_single_tenant_resolver_ignores_untrusted_tenant_shaped_attributes() -> None:
    """当前认证域未建 tenant 字段，不能把任意同名属性当授权事实。"""
    authenticated_user = SimpleNamespace(user_id=1, tenant_id=999)

    assert DEFAULT_TENANT_ID == 0
    assert resolve_tenant_id(authenticated_user) == DEFAULT_TENANT_ID
