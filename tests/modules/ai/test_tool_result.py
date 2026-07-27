"""UIResult + ToolResult.ui 字段测试（spec 2026-07-16-tool-result-view-design.md §2.1/§2.2）。"""

from app.modules.ai.agents.gateway.result import ToolResult, UIResult


class TestUIResult:
    def test_construct_with_all_fields(self):
        ui = UIResult(
            view_type="rows_affected",
            view_data={"count": 2, "ids": ["u1", "u2"]},
            audit={"affected_user_ids": ["u1", "u2"]},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": 2},
        )
        assert ui.view_type == "rows_affected"
        assert ui.view_data["count"] == 2
        assert ui.audit["affected_user_ids"] == ["u1", "u2"]
        assert ui.label_key == "ai.tool.user.batch_delete.result"
        assert ui.label_params == {"count": 2}

    def test_default_audit_and_label_are_empty(self):
        ui = UIResult(view_type="plain_json", view_data={"count": 5})
        assert ui.audit == {}
        assert ui.label_key == ""
        assert ui.label_params == {}


class TestToolResultUi:
    def test_success_with_ui(self):
        ui = UIResult(view_type="plain_json", view_data={"count": 5})
        r = ToolResult.success(data={"count": 5}, ui=ui)
        assert r.ok is True
        assert r.data == {"count": 5}
        assert r.ui is not None
        assert r.ui.view_type == "plain_json"

    def test_success_without_ui_returns_none_ui(self):
        """决策 3 修正：ui 可选，向后兼容现有 executor / 第三方 tool。
        builtin tool 强制带 ui 由 lint（Task 9）保证，不靠 ToolResult.success 签名。
        """
        r = ToolResult.success(data={"count": 5})
        assert r.ok is True
        assert r.data == {"count": 5}
        assert r.ui is None

    def test_failure_does_not_require_ui(self):
        r = ToolResult.failure(
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="target not in scope",
        )
        assert r.ok is False
        assert r.ui is None
