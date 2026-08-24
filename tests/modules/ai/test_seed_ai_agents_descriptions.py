"""验证 seed_ai_agents.py 的 Agent 描述具有足够区分度。"""

import importlib.util
import re
from pathlib import Path


def _load_agents_list():
    """从 scripts/seed_ai_agents.py 读 AGENT_SEED 常量，不实际执行 seed."""
    spec = importlib.util.spec_from_file_location(
        "seed_ai_agents",
        Path(__file__).parent.parent.parent.parent / "scripts" / "seed_ai_agents.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AGENT_SEED


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_ai_agents_flags",
        Path(__file__).parent.parent.parent.parent / "scripts" / "seed_ai_agents.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_each_agent_description_50_to_200_chars():
    """每个 description 长度应在 50-200 字。"""
    agents = _load_agents_list()
    for agent in agents:
        desc_len = len(agent["description"])
        assert 50 <= desc_len <= 200, (
            f"Agent {agent['code']} description 长度 {desc_len} 不在 50-200 范围"
        )


def test_each_description_has_boundary_clause():
    """除 shared 外，description 应包含与相邻 Agent 的边界声明。"""
    agents = _load_agents_list()
    for agent in agents:
        if agent["code"] == "shared":
            continue
        assert any(
            kw in agent["description"] for kw in ("边界", "归", "涉及", "范围")
        ), f"Agent {agent['code']} 缺少边界声明"


def test_each_description_has_typical_query():
    """description 应包含至少两个用单引号标记的典型查询示例。

    原 `assert "query" in desc` 太弱（所有 description 写"典型 query："都通过，
    无防御能力）. 改为统计单引号 quoted example 数量，要求 ≥2.
    """
    agents = _load_agents_list()
    quoted_example_re = re.compile(r"'[^']+'")
    for agent in agents:
        examples = quoted_example_re.findall(agent["description"])
        assert len(examples) >= 2, (
            f"Agent {agent['code']} description 仅含 {len(examples)} 个 "
            f"缺少至少两个引号查询示例：{agent['description']!r}"
        )


def test_seed_contains_seven_agents():
    """种子包含 shared 和六个业务 Agent。"""
    agents = _load_agents_list()
    codes = {a["code"] for a in agents}
    expected = {
        "shared",
        "user_mgmt",
        "role_mgmt",
        "config_mgmt",
        "dept_mgmt",
        "provider_mgmt",
        "job_mgmt",
    }
    assert codes == expected


def test_phase3_complete_agents_default_enabled_on_fresh_insert() -> None:
    mod = _load_seed_module()

    assert mod.PUBLISHED_AGENT_CODES == {
        "shared",
        "user_mgmt",
        "role_mgmt",
        "dept_mgmt",
    }


def test_phase3_agent_inventory_describes_current_capabilities() -> None:
    mod = _load_seed_module()
    inventory = {item["code"]: item["description"] for item in mod.AGENT_SEED}

    assert "尚未发布" not in inventory["user_mgmt"]
    assert "完整部门" in inventory["user_mgmt"]
    assert "受限委派" in inventory["role_mgmt"]
    assert "移动部门" in inventory["dept_mgmt"]


def test_unpublished_descriptions_do_not_claim_availability() -> None:
    mod = _load_seed_module()
    unpublished = {
        item["code"]: item["description"]
        for item in mod.AGENT_SEED
        if item["code"] not in mod.PUBLISHED_AGENT_CODES
    }

    assert unpublished
    assert all("尚未发布" in description for description in unpublished.values())
