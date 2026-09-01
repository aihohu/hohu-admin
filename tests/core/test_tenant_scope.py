import pytest
from sqlalchemy.dialects import postgresql

from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_cache_key, tenant_select
from app.modules.system.models.role import Role


@pytest.fixture
def tenant() -> TenantContext:
    return TenantContext(
        tenant_id=42,
        tenant_code="tenant-a",
        actor_user_id=1001,
        tenant_version=1,
        source="access_token",
    )


def test_tenant_select_always_starts_with_the_trusted_tenant_predicate(tenant):
    statement = tenant_select(Role, tenant=tenant)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )

    assert "sys_role.tenant_id = 42" in str(compiled)


def test_tenant_cache_key_cannot_be_built_without_a_context(tenant):
    assert tenant_cache_key(tenant, "system", "username", 1001) == (
        "tenant:42:system:username:1001"
    )
    with pytest.raises(TypeError):
        tenant_cache_key(None, "system", "username", 1001)
