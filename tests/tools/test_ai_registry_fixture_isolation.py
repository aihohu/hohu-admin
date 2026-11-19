"""Registry unit fixtures must preserve tools used by later test files."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RegistryFixtureIsolationTests(unittest.TestCase):
    def test_real_fixture_teardown_restores_builtin_registry(self):
        # A separate interpreter prevents this regression from changing the
        # surrounding suite's registry or relying on an earlier test's imports.
        script = """
import importlib
import inspect
import pytest
from app.modules.ai.agents.tools import (
    ToolRegistry, compute_available_tools, load_builtin_tools,
)

class ExpectedBodyFailure(Exception):
    pass

for name in ('test_gateway', 'test_pydantic_ai_wrapper', 'test_tool_registry'):
    module = importlib.import_module('tests.modules.ai.' + name)
    for fail_body in (False, True):
        ToolRegistry.reset()
        load_builtin_tools()
        original = ToolRegistry.get()
        exported = original.find('user.export')
        departments = compute_available_tools(
            {'system:dept:list'}, 'dept_mgmt', enabled_extra=[],
        )
        assert exported is not None and departments
        try:
            with pytest.MonkeyPatch.context() as patch:
                raw_fixture = module.reset_registry.__wrapped__
                fixture = (
                    raw_fixture(patch) if inspect.signature(raw_fixture).parameters
                    else raw_fixture()
                )
                next(fixture)
                try:
                    assert ToolRegistry.get() is not original
                    assert not ToolRegistry.get().all()
                    # Registry unit tests may themselves reset the singleton.
                    ToolRegistry.reset()
                    if fail_body:
                        raise ExpectedBodyFailure()
                finally:
                    next(fixture, None)
        except ExpectedBodyFailure:
            pass
        assert ToolRegistry.get() is original, (name, fail_body, 'lost registry')
        assert ToolRegistry.get().find('user.export') is exported
        assert compute_available_tools(
            {'system:dept:list'}, 'dept_mgmt', enabled_extra=[],
        ) == departments
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=os.environ
            | {
                "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "tests"),
                "ENV": "test",
                "DATABASE_URL": "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
                "REDIS_HOST": "127.0.0.1",
                "REDIS_PORT": "1",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
