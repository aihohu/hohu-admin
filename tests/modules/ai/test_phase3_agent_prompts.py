from scripts.seed_agent_prompts import DEFAULT_PROMPTS, LEGACY_DEFAULT_PROMPTS


def test_role_agent_prompt_covers_complete_phase3_workflow() -> None:
    prompt = DEFAULT_PROMPTS["role_mgmt"]

    for tool_name in (
        "role.count",
        "role.list",
        "role.lookup",
        "role.create",
        "role.update",
        "role.update_menus",
        "role.update_agents",
    ):
        assert tool_name in prompt
    assert "完整 menu_ids" in prompt
    assert "完整 agent_ids" in prompt
    assert "零命中" in prompt
    assert "多命中" in prompt
    assert "禁止猜测" in prompt


def test_dept_agent_prompt_covers_scoped_phase3_workflow() -> None:
    prompt = DEFAULT_PROMPTS["dept_mgmt"]

    for tool_name in (
        "dept.count",
        "dept.list",
        "dept.lookup",
        "dept.create",
        "dept.update",
        "dept.move",
    ):
        assert tool_name in prompt
    assert "零命中" in prompt
    assert "多命中" in prompt
    assert "禁止猜测" in prompt
    assert "根部门" in prompt


def test_phase3_known_defaults_are_safe_to_upgrade() -> None:
    assert LEGACY_DEFAULT_PROMPTS["role_mgmt"]
    assert LEGACY_DEFAULT_PROMPTS["dept_mgmt"]
    assert DEFAULT_PROMPTS["role_mgmt"] not in LEGACY_DEFAULT_PROMPTS["role_mgmt"]
    assert DEFAULT_PROMPTS["dept_mgmt"] not in LEGACY_DEFAULT_PROMPTS["dept_mgmt"]
