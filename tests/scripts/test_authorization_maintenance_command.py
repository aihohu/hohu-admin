"""Only bounded operator inventories and fixed actions reach maintenance owners."""

import json
import os
from dataclasses import asdict

import pytest

from app.core.exceptions import BusinessException
from app.core.process_identity import ProcessIdentity
from tools.ops import authorization_maintenance as ops


def _inventory():
    return {
        "manifest": {
            "deployment_id": "unit",
            "build_sha": "a" * 40,
            "mode": "direct",
            "surfaces": ["api", "scheduler"],
        },
        "application_roles": ["application"],
        "processes": {
            "mode": "direct",
            "identities": [{"pid": 987654321, "birth": "windows:123"}],
        },
    }


@pytest.mark.parametrize(
    "invalid", ["oversize", "extra", "callback", "empty", "boolean_pid"]
)
def test_inventory_rejects_unknown_and_unbounded_input(tmp_path, invalid):
    value = _inventory()
    if invalid == "extra":
        value["factory"] = "arbitrary.callable"
    elif invalid == "callback":
        value["processes"]["observer"] = "arbitrary.callable"
    elif invalid == "empty":
        value["processes"]["identities"] = []
    elif invalid == "boolean_pid":
        value["processes"]["identities"][0]["pid"] = True
    path = tmp_path / "inventory.json"
    path.write_text(" " * 65537 if invalid == "oversize" else json.dumps(value))
    with pytest.raises(BusinessException):
        ops.read_inventory(path)


def test_direct_inventory_cannot_omit_live_registered_process(tmp_path):
    process = ProcessIdentity.read(os.getpid())
    (tmp_path / ("process-" + str(process.pid) + ".json")).write_text(
        json.dumps(
            {
                "identity": asdict(process),
                "role": "all",
                "state": "starting",
                "build_sha": "a" * 40,
                "active_tasks": 0,
            }
        )
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory()))
    _, processes, _ = ops.read_inventory(inventory)
    with pytest.raises(BusinessException):
        ops.require_registered_inventory(tmp_path, processes)


@pytest.mark.parametrize("action", ["migration", "eval", "seed.system;rm", "factory"])
def test_command_cannot_select_arbitrary_action(action):
    with pytest.raises(SystemExit):
        ops.arguments(["execute", "--inventory", "inventory.json", "--action", action])
