"""Deployment seeds are complete, repeatable and owned by one transaction."""

import pytest
from sqlalchemy import select

from app.core.id_generator import next_id
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.seed_prompts import DEFAULT_PROMPTS
from app.modules.system.models.menu import Menu
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from scripts import init_db, seed_settings, sync_menus
from scripts.seed_ai_agents import seed_ai_agents_in_session
from tests.tenant_helpers import create_test_tenant


def test_settings_accepts_bootstrap_secret_without_exposing_it(tmp_path):
    from app.core.config import Settings  # noqa: PLC0415

    env_file = tmp_path / ".env"
    env_file.write_text("HOHU_ADMIN_PASSWORD=PrivateSeed123\n", encoding="utf-8")
    settings = Settings(
        _env_file=env_file,
        ENV="dev",
        DATABASE_URL="postgresql+asyncpg://unused",
        SECRET_KEY="test",
    )
    assert settings.HOHU_ADMIN_PASSWORD.get_secret_value() == "PrivateSeed123"
    assert "PrivateSeed123" not in repr(settings)


async def test_agent_seed_fills_defaults_and_preserves_custom_prompt_and_disable(
    db_session,
):
    await seed_ai_agents_in_session(db_session)
    agent = await db_session.scalar(select(AiAgent).where(AiAgent.code == "user_mgmt"))
    agent.system_prompt = ""
    await db_session.flush()
    await seed_ai_agents_in_session(db_session)
    assert agent.system_prompt == DEFAULT_PROMPTS["user_mgmt"]
    agent.system_prompt = "Deployment-specific business instructions"
    agent.enabled = False
    await db_session.flush()
    await seed_ai_agents_in_session(db_session)
    assert agent.system_prompt == "Deployment-specific business instructions"
    assert agent.enabled is False


def test_fresh_menus_use_the_complete_sync_catalog():
    menus = sync_menus.build_initial_menus(tenant_id=71)
    assert {m.route_name for m in menus if m.route_name} == {
        d["route_name"] for d in sync_menus.MENU_DEFINITIONS if d["menu_type"] != "F"
    }
    assert all(m.tenant_id == 71 for m in menus)
    second = sync_menus.build_initial_menus(tenant_id=72)
    assert {m.menu_id for m in menus}.isdisjoint(m.menu_id for m in second)


@pytest.mark.parametrize("hosted", [False, True])
async def test_sync_keeps_new_system_groups_on_rerun(db_session, monkeypatch, hosted):
    tenant = await create_test_tenant(db_session, prefix="seedgroups")
    monkeypatch.setattr(sync_menus, "DEFAULT_TENANT_ID", tenant.tenant_id)
    for _ in range(2):
        menus = await sync_menus.sync_menus_in_session(
            db_session, tenant_id=tenant.tenant_id, hosted=hosted
        )
        routes = {m.route_name: m for m in menus if m.route_name}
        system = routes["system"]
        visible = sorted(
            (m for m in menus if m.parent_id == system.menu_id and not m.hide_in_menu),
            key=lambda m: m.order,
        )
        assert [m.route_name for m in visible] == [
            "system_setting",
            "system_dict",
            "system_config",
            "system_file",
            "system_monitor",
            "system_demo",
        ]
        assert routes["dashboard"].parent_id == routes["system_demo"].menu_id
        for name in ("system_operation-log", "system_login-log"):
            assert routes[name].parent_id == routes["system_monitor"].menu_id
        if not hosted:
            assert (
                routes["system_data-scope-demo"].parent_id
                == routes["system_demo"].menu_id
            )
        assert not any(name.startswith("marketplace") for name in routes)
        assert "app-management" not in routes


async def test_sync_preserves_existing_marketplace_and_system_records(
    db_session, monkeypatch
):
    from app.db.base import role_menus  # noqa: PLC0415
    from app.modules.system.models.role import Role  # noqa: PLC0415

    tenant = await create_test_tenant(db_session, prefix="seedlegacy")
    monkeypatch.setattr(sync_menus, "DEFAULT_TENANT_ID", tenant.tenant_id)
    existing = [
        Menu(
            menu_id=next_id(),
            tenant_id=tenant.tenant_id,
            route_name=name,
            menu_name=f"Custom {name}",
            menu_type="M",
            order=42,
            status="2",
        )
        for name in ("marketplace", "marketplace-detail", "app-management", "system")
    ]
    for name, parent in (
        ("marketplace_installed", existing[2]),
        ("system_setting", existing[3]),
    ):
        existing.append(
            Menu(
                menu_id=next_id(),
                tenant_id=tenant.tenant_id,
                route_name=name,
                menu_name=f"Custom {name}",
                menu_type="C",
                parent_id=parent.menu_id,
                order=37,
                status="2",
            )
        )
    existing.append(
        Menu(
            menu_id=next_id(),
            tenant_id=tenant.tenant_id,
            menu_name="Custom install",
            menu_type="F",
            permission="marketplace:install",
            parent_id=existing[4].menu_id,
            order=9,
            status="2",
        )
    )
    role = Role(
        role_id=next_id(),
        tenant_id=tenant.tenant_id,
        role_name="Legacy",
        role_code="legacy",
        status="1",
    )
    db_session.add_all([*existing, role])
    await db_session.flush()
    await db_session.execute(
        role_menus.insert().values(
            tenant_id=tenant.tenant_id,
            role_id=role.role_id,
            menu_id=existing[-1].menu_id,
        )
    )

    def snapshot(menu):
        return (
            menu.menu_id,
            menu.menu_name,
            menu.parent_id,
            menu.order,
            menu.status,
            menu.permission,
        )

    before = [snapshot(m) for m in existing]
    for _ in range(2):
        await sync_menus.sync_menus_in_session(db_session, tenant_id=tenant.tenant_id)
        for menu in existing:
            await db_session.refresh(menu)
        assert [snapshot(m) for m in existing] == before
        assert (
            await db_session.scalar(
                select(role_menus.c.menu_id).where(
                    role_menus.c.tenant_id == tenant.tenant_id,
                    role_menus.c.role_id == role.role_id,
                )
            )
            == existing[-1].menu_id
        )


async def test_config_seed_is_tenant_scoped_and_preserves_custom_values(db_session):
    first = await create_test_tenant(db_session, prefix="seedca")
    second = await create_test_tenant(db_session, prefix="seedcb")
    db_session.add(
        SystemSetting(
            setting_id=next_id(),
            tenant_id=first.tenant_id,
            setting_key="site_name",
            setting_value="Custom",
            status="1",
        )
    )
    await db_session.flush()
    for _ in range(2):
        await seed_settings.seed_settings_in_session(
            db_session, tenant_id=first.tenant_id
        )
        await seed_settings.seed_settings_in_session(
            db_session, tenant_id=second.tenant_id
        )
    rows = list(
        (
            await db_session.scalars(
                select(SystemSetting).where(
                    SystemSetting.tenant_id.in_([first.tenant_id, second.tenant_id]),
                    SystemSetting.setting_key == "site_name",
                )
            )
        ).all()
    )
    assert len(rows) == 2
    assert (
        next(c for c in rows if c.tenant_id == first.tenant_id).setting_value
        == "Custom"
    )


async def test_fresh_seed_and_rerun_do_not_reset_password(db_session, monkeypatch):
    tenant = await create_test_tenant(db_session, prefix="seed-fresh")
    monkeypatch.setattr(init_db, "DEFAULT_TENANT_ID", tenant.tenant_id)
    await init_db.seed_database(db_session, admin_password="FirstSeed123")
    user = await db_session.scalar(
        select(User).where(
            User.tenant_id == tenant.tenant_id, User.user_name == "admin"
        )
    )
    assert user is not None
    original_hash = user.hashed_password
    await init_db.seed_database(db_session, admin_password="SecondSeed123")
    assert user.hashed_password == original_hash
    user.user_name = "renamedadmin"
    await db_session.flush()
    await init_db.seed_database(db_session)
    assert user.user_name == "renamedadmin"
    menus = list(
        (
            await db_session.scalars(
                select(Menu).where(Menu.tenant_id == tenant.tenant_id)
            )
        ).all()
    )
    assert len(menus) == len(sync_menus.MENU_DEFINITIONS)
    configs = {
        c.setting_key: c.setting_value
        for c in (
            await db_session.scalars(
                select(SystemSetting).where(SystemSetting.tenant_id == tenant.tenant_id)
            )
        ).all()
    }
    assert configs["ai:enabled_tools"] == '["file.parse"]'
    assert "site_name" in configs


async def test_missing_bootstrap_password_does_not_write(db_session, monkeypatch):
    tenant = await create_test_tenant(db_session, prefix="seednp")
    monkeypatch.setattr(init_db, "DEFAULT_TENANT_ID", tenant.tenant_id)
    with pytest.raises(ValueError, match="HOHU_ADMIN_PASSWORD"):
        await init_db.seed_database(db_session, admin_password=None)
    assert (
        await db_session.scalar(select(User).where(User.tenant_id == tenant.tenant_id))
        is None
    )


async def test_failed_seed_rolls_back_menu_and_config_writes(db_session, monkeypatch):
    tenant = await create_test_tenant(db_session, prefix="seedfail")
    monkeypatch.setattr(init_db, "DEFAULT_TENANT_ID", tenant.tenant_id)

    async def fail(_db):
        raise RuntimeError("agent-seed-failed")

    monkeypatch.setattr(init_db, "seed_ai_agents_in_session", fail)
    with pytest.raises(RuntimeError, match="agent-seed-failed"):
        async with db_session.begin_nested():
            await init_db.seed_database(db_session, admin_password="FirstSeed123")
    assert (
        await db_session.scalar(select(Menu).where(Menu.tenant_id == tenant.tenant_id))
        is None
    )
    assert (
        await db_session.scalar(
            select(SystemSetting).where(SystemSetting.tenant_id == tenant.tenant_id)
        )
        is None
    )


async def test_hosted_sync_preserves_custom_menu_and_does_not_grant_permissions(
    db_session,
):
    from app.db.base import role_menus  # noqa: PLC0415
    from app.modules.system.hosted_menu_seed import HOSTED_ROUTE_NAMES  # noqa: PLC0415

    tenant = await create_test_tenant(db_session, prefix="seedhost")
    custom = Menu(
        tenant_id=tenant.tenant_id,
        menu_name="Custom",
        menu_type="C",
        route_name="custom_page",
        status="2",
        order=4,
    )
    db_session.add(custom)
    await db_session.flush()
    for _ in range(2):
        menus = await sync_menus.sync_menus_in_session(
            db_session, tenant_id=tenant.tenant_id, hosted=True
        )
    routes = {menu.route_name for menu in menus if menu.route_name}
    assert routes == HOSTED_ROUTE_NAMES | {"custom_page"}
    assert custom.status == "2"
    assert (
        await db_session.scalar(
            select(role_menus.c.role_id).where(
                role_menus.c.tenant_id == tenant.tenant_id
            )
        )
        is None
    )
