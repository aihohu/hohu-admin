"""Runtime prompt metadata must not copy business identity without a source."""

from types import SimpleNamespace

from app.modules.ai.agents.safety_preamble import build_dynamic_block


def test_dynamic_prompt_does_not_bypass_identity_fields_or_expose_scope_counts():
    deps = SimpleNamespace(
        user=SimpleNamespace(user_name="PRIVATE-ACTOR-NAME", user_id=998877665544),
        data_scope=SimpleNamespace(
            accessible_dept_ids={1, 2, 3},
            accessible_user_scope=None,
            scope_kinds=frozenset(),
        ),
        perms={"system:user:list"},
        trace_id="tr_safe_test",
    )
    value = build_dynamic_block(deps)
    assert "PRIVATE-ACTOR-NAME" not in value and "998877665544" not in value
    assert "3 个部门" not in value
    assert "system:user:list" in value and "tr_safe_test" in value
