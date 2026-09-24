"""Deployment seeds are complete, repeatable and owned by one transaction."""

import pytest
from sqlalchemy import select

from app.core.id_generator import next_id
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.seed_prompts import DEFAULT_PROMPTS
from app.modules.system.models.config import Config
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from scripts import init_db, seed_config, sync_menus
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


async def test_config_seed_is_tenant_scoped_and_preserves_custom_values(db_session):
    first = await create_test_tenant(db_session, prefix="seedca")
    second = await create_test_tenant(db_session, prefix="seedcb")
    db_session.add(
        Config(
            config_id=next_id(),
            tenant_id=first.tenant_id,
            config_key="site_name",
            config_name="Site",
            config_value="Custom",
            config_type="text",
            config_group="basic",
            status="1",
            is_public=True,
        )
    )
    await db_session.flush()
    for _ in range(2):
        await seed_config.seed_config_in_session(db_session, tenant_id=first.tenant_id)
        await seed_config.seed_config_in_session(db_session, tenant_id=second.tenant_id)
    rows = list(
        (
            await db_session.scalars(
                select(Config).where(
                    Config.tenant_id.in_([first.tenant_id, second.tenant_id]),
                    Config.config_key == "site_name",
                )
            )
        ).all()
    )
    assert len(rows) == 2
    assert (
        next(c for c in rows if c.tenant_id == first.tenant_id).config_value == "Custom"
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
        c.config_key: c.config_value
        for c in (
            await db_session.scalars(
                select(Config).where(Config.tenant_id == tenant.tenant_id)
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
            select(Config).where(Config.tenant_id == tenant.tenant_id)
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
