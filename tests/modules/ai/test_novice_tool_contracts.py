"""Browser failures: lookup bounds must be visible to the model."""

import pytest
from pydantic import ValidationError

from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.pydantic_ai_wrapper import wrap_tool_for_pydantic_ai
from app.modules.ai.agents.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    "name",
    [
        "dept.lookup",
        "user.dept_lookup",
        "role.lookup",
        "role.menu_lookup",
        "role.agent_lookup",
        "user.role_lookup",
    ],
)
def test_lookup_schema_advertises_its_actual_bounds(name):
    load_builtin_tools()
    tool = wrap_tool_for_pydantic_ai(ToolRegistry.get().find(name))
    schema = tool.function_schema.json_schema["properties"]["limit"]
    assert schema["minimum"] == 1
    assert schema["maximum"] == 20


@pytest.mark.parametrize(
    "name",
    ["dept.list", "role.list", "user.list", "dept.count", "role.count", "user.count"],
)
def test_list_status_schema_rejects_invalid_values_before_gateway(name):
    load_builtin_tools()
    tool = wrap_tool_for_pydantic_ai(ToolRegistry.get().find(name))
    validator = tool.function_schema.validator
    assert validator.validate_python({"filters": {}})["filters"] == {}
    for value in [None, "all", "enabled", "0"]:
        with pytest.raises(ValidationError):
            validator.validate_python({"filters": {"status": value}})


@pytest.mark.parametrize(
    "name,field", [("user.stats", "group_by"), ("user.distinct", "field")]
)
def test_aggregate_fields_expose_the_whitelist_to_the_model(name, field):
    load_builtin_tools()
    validator = wrap_tool_for_pydantic_ai(
        ToolRegistry.get().find(name)
    ).function_schema.validator
    for value in [None, "dept_id", "user_name", "password"]:
        with pytest.raises(ValidationError):
            validator.validate_python({field: value})
    assert validator.validate_python({field: "status"})[field] == "status"
