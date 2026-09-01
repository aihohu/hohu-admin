"""Tenant isolation for configuration, dictionary, and menu metadata."""

from app.core.id_generator import next_id
from app.modules.system.models.config import Config
from app.modules.system.models.dict_type import DictType
from app.modules.system.models.menu import Menu
from app.modules.system.service.config_service import config_service
from app.modules.system.service.dict_type_service import dict_type_service
from app.modules.system.service.menu_service import menu_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_metadata_keys_repeat_across_tenants_without_read_leak(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="metadata-b")
    marker = next_id()
    key = f"shared.config.{marker}"
    dict_code = f"shared_dict_{marker}"
    config_a = Config(
        tenant_id=0,
        config_name="Shared config",
        config_key=key,
        config_value="A",
        config_group="test",
        status="1",
    )
    config_b = Config(
        tenant_id=tenant_b.tenant_id,
        config_name="Shared config",
        config_key=key,
        config_value="B",
        config_group="test",
        status="1",
    )
    dict_a = DictType(
        tenant_id=0,
        dict_name=f"Shared dict {marker}",
        dict_type=dict_code,
        status="1",
    )
    dict_b = DictType(
        tenant_id=tenant_b.tenant_id,
        dict_name=dict_a.dict_name,
        dict_type=dict_code,
        status="1",
    )
    menu_a = Menu(
        tenant_id=0,
        menu_name=f"Shared menu {marker}",
        menu_type="F",
        permission=f"test:a:{marker}",
        status="1",
    )
    menu_b = Menu(
        tenant_id=tenant_b.tenant_id,
        menu_name=menu_a.menu_name,
        menu_type="F",
        permission=f"test:b:{marker}",
        status="1",
    )
    db_session.add_all([config_a, config_b, dict_a, dict_b, menu_a, menu_b])
    await db_session.flush()

    tenant_a_ctx = tenant_context()
    tenant_b_ctx = tenant_context(tenant_id=tenant_b.tenant_id)
    assert await config_service.get_value(db_session, key, tenant=tenant_a_ctx) == "A"
    assert await config_service.get_value(db_session, key, tenant=tenant_b_ctx) == "B"
    assert dict_a in await dict_type_service.get_all_enabled(
        db_session, tenant=tenant_a_ctx
    )
    assert dict_b in await dict_type_service.get_all_enabled(
        db_session, tenant=tenant_b_ctx
    )
    assert menu_b not in await menu_service.get_all_menus(
        db_session, tenant=tenant_a_ctx
    )
