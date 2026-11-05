"""Existing script names dispatch only through verified maintenance entrypoints."""

import importlib
from unittest.mock import AsyncMock

import pytest

from tools.ops import authorization_maintenance as ops


@pytest.mark.parametrize(
    "module,function,action",
    [
        ("init_db", "init_db", "seed.system"),
        ("seed_settings", "seed_settings", "seed.settings"),
        ("seed_ai_agents", "seed_ai_agents", "seed.agents"),
        ("sync_menus", "sync_menus", "seed.menus"),
    ],
)
async def test_seed_script_never_creates_unplanned_writer(
    monkeypatch, module, function, action
):
    script = importlib.import_module("scripts." + module)
    dispatch = AsyncMock(return_value={"state": "stopped"})
    monkeypatch.setattr(ops, "run_seed_entry", dispatch, raising=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("seed script opened an ordinary database writer")

    monkeypatch.setattr(script, "create_async_engine", forbidden, raising=False)
    await getattr(script, function)()
    dispatch.assert_awaited_once_with(action)


def test_seed_script_requires_inventory_and_cannot_change_action():
    with pytest.raises(SystemExit):
        ops.seed_arguments("seed.settings", [])
    with pytest.raises(SystemExit):
        ops.seed_arguments(
            "seed.settings",
            ["--inventory", "deployment.json", "--action", "principal.create"],
        )
    args = ops.seed_arguments("seed.settings", ["--inventory", "deployment.json"])
    assert (
        args.command == "execute"
        and args.action == "seed.settings"
        and args.payload is None
    )
