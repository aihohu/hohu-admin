"""spec §7.3: seed_ai_agents.py 维护高区分度 description（路由准确率唯一关键变量）."""

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


def test_each_agent_description_50_to_200_chars():
    """spec §7.3: 每个 description 应在 50-200 字."""
    agents = _load_agents_list()
    for agent in agents:
        desc_len = len(agent["description"])
        assert 50 <= desc_len <= 200, (
            f"Agent {agent['code']} description 长度 {desc_len} 不在 50-200 范围"
        )


def test_each_description_has_boundary_clause():
    """spec §7.3: description 应含与相邻 Agent 的边界声明（shared 除外）."""
    agents = _load_agents_list()
    for agent in agents:
        if agent["code"] == "shared":
            continue
        assert any(
            kw in agent["description"] for kw in ("边界", "归", "涉及", "范围")
        ), f"Agent {agent['code']} 缺少边界声明"


def test_each_description_has_typical_query():
    """spec §7.3: description 应含 ≥2 个典型 query 示例（用单引号包 'xxx'）.

    原 `assert "query" in desc` 太弱（所有 description 写"典型 query："都通过，
    无防御能力）. 改为统计单引号 quoted example 数量，要求 ≥2.
    """
    agents = _load_agents_list()
    quoted_example_re = re.compile(r"'[^']+'")
    for agent in agents:
        examples = quoted_example_re.findall(agent["description"])
        assert len(examples) >= 2, (
            f"Agent {agent['code']} description 仅含 {len(examples)} 个 "
            f"'xxx' quoted example，spec §7.3 要求 ≥2. 内容：{agent['description']!r}"
        )


def test_seed_contains_seven_agents():
    """spec §1: 7 个内置 Agent（shared + 6 业务）."""
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
