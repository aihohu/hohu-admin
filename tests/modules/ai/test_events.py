"""SSE 5 类事件 + event_to_sse_data 单元测试 — spec §8.1

字段命名决策：顶层 camelCase（与项目其他 API 响应一致），args 嵌套保留
snake_case（与 LLM tool schema 一致）。
"""

# ruff: noqa: PLC0415

import json

from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    ConfirmationRequiredEvent,
    DoneEvent,
    DryRunSummary,
    ToolCallResultEvent,
    ToolCallStartedEvent,
    event_to_sse_data,
)


def _started(**overrides) -> ToolCallStartedEvent:
    """构造 started 事件，提供默认值，单测里只关心变更字段"""
    defaults = {
        "tool": "user.count",
        "tool_call_id": "tc_abc",
        "summary": "tool=user.count, risk=low, mode=autonomous",
        "args": {"status": "1"},
        "risk": "low",
        "trace_id": "tr_test_001",
    }
    defaults.update(overrides)
    return ToolCallStartedEvent(**defaults)


def _result(**overrides) -> ToolCallResultEvent:
    """构造 result 事件，提供默认值"""
    defaults = {
        "tool": "user.count",
        "tool_call_id": "tc_abc",
        "ok": True,
        "duration_ms": 100,
        "result": {"count": 5},
    }
    defaults.update(overrides)
    return ToolCallResultEvent(**defaults)


class TestCamelCaseKeys:
    """spec §8.1: SSE 事件顶层字段全部 camelCase（决策记录在 events.py docstring）"""

    def test_started_camel_case(self) -> None:
        data = json.loads(event_to_sse_data(_started()))
        assert "toolCallId" in data
        assert "tool_call_id" not in data

    def test_result_camel_case(self) -> None:
        data = json.loads(event_to_sse_data(_result()))
        assert "toolCallId" in data
        assert "durationMs" in data
        assert "tool_call_id" not in data
        assert "duration_ms" not in data

    def test_confirmation_camel_case(self) -> None:
        event = ConfirmationRequiredEvent(
            confirmation_id="conf_x",
            tool="user.batch_delete",
            tool_call_id="tc_bd",
            summary="tool=user.batch_delete, risk=destructive, mode=hitl",
            args={"user_ids": [1, 2, 3]},
            expires_at="2026-07-04T14:07:30Z",
        )
        data = json.loads(event_to_sse_data(event))
        assert "confirmationId" in data
        assert "expiresAt" in data
        assert "confirmation_id" not in data
        assert "expires_at" not in data

    def test_error_camel_case(self) -> None:
        event = AiErrorEvent(error_code="LLM_API_ERROR", message="provider timeout")
        data = json.loads(event_to_sse_data(event))
        assert "errorCode" in data
        assert "error_code" not in data

    def test_args_keeps_snake_case(self) -> None:
        """args 嵌套字段保留 snake_case（LLM schema 一致性）"""
        event = _started(args={"user_id": 42, "new_dept_id": 8})
        data = json.loads(event_to_sse_data(event))
        # 顶层 toolCallId camelCase
        assert data["toolCallId"] == "tc_abc"
        # 嵌套 args user_id 保留 snake_case
        assert data["args"] == {"user_id": 42, "new_dept_id": 8}

    def test_dry_run_camel_case(self) -> None:
        """dryRun 是事件元字段，camelCase；嵌套 affectedCount 也转"""
        event = ConfirmationRequiredEvent(
            confirmation_id="conf_abc",
            tool="user.update_dept",
            tool_call_id="tc_z",
            summary="tool=user.update_dept, risk=high, mode=hitl",
            args={"user_id": 42, "new_dept_id": 8},
            expires_at="2026-07-04T14:07:30Z",
            dry_run=DryRunSummary(summary="将影响 1 行", affected_count=1),
        )
        data = json.loads(event_to_sse_data(event))
        assert "dryRun" in data
        assert "dry_run" not in data
        assert data["dryRun"]["affectedCount"] == 1
        assert "affected_count" not in data["dryRun"]


class TestStartedRisk:
    """risk 字段透传（spec §5.3 风险分级）"""

    def test_low_risk_emitted(self) -> None:
        data = json.loads(event_to_sse_data(_started(risk="low")))
        assert data["risk"] == "low"

    def test_high_risk_emitted(self) -> None:
        data = json.loads(event_to_sse_data(_started(risk="high")))
        assert data["risk"] == "high"

    def test_destructive_risk_emitted(self) -> None:
        data = json.loads(
            event_to_sse_data(_started(tool="user.batch_delete", risk="destructive"))
        )
        assert data["risk"] == "destructive"

    def test_trace_id_emitted_camel_case(self) -> None:
        """spec §8.7: traceId 暴露给前端用于 chip 跳转回放"""
        data = json.loads(event_to_sse_data(_started(trace_id="tr_abc123")))
        assert data["traceId"] == "tr_abc123"
        assert "trace_id" not in data


class TestResultDurationAndRows:
    """duration_ms 必填 / affected_rows 可选"""

    def test_duration_ms_emitted(self) -> None:
        data = json.loads(event_to_sse_data(_result(duration_ms=230)))
        assert data["durationMs"] == 230

    def test_affected_rows_emitted_when_present(self) -> None:
        data = json.loads(event_to_sse_data(_result(affected_rows=3)))
        assert data["affectedRows"] == 3

    def test_affected_rows_omitted_when_none(self) -> None:
        """affected_rows=None 时字段剔除（前端 v-if 据此决定是否显示「N 行」）"""
        sse = event_to_sse_data(_result(affected_rows=None))
        assert "affectedRows" not in sse
        assert "affected_rows" not in sse


class TestEventDataclasses:
    def test_tool_call_started_serialization(self) -> None:
        event = _started()
        data = json.loads(event_to_sse_data(event))
        assert data["type"] == "tool_call_started"
        assert data["tool"] == "user.count"
        assert data["toolCallId"] == "tc_abc"
        assert data["args"] == {"status": "1"}

    def test_tool_call_result_success(self) -> None:
        event = _result()
        data = json.loads(event_to_sse_data(event))
        assert data["type"] == "tool_call_result"
        assert data["ok"] is True
        assert data["result"] == {"count": 5}
        assert "errorCode" not in data
        assert "errorMsg" not in data

    def test_tool_call_result_failure(self) -> None:
        event = _result(
            ok=False,
            duration_ms=8,
            result=None,
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="目标不在你的可见范围内",
        )
        data = json.loads(event_to_sse_data(event))
        assert data["ok"] is False
        assert data["errorCode"] == "AI_DATA_SCOPE_VIOLATION"
        assert data["durationMs"] == 8
        assert "result" not in data  # None 字段剔除

    def test_confirmation_required_with_dry_run(self) -> None:
        event = ConfirmationRequiredEvent(
            confirmation_id="conf_abc",
            tool="user.update_dept",
            tool_call_id="tc_z",
            summary="tool=user.update_dept, risk=high, mode=hitl",
            args={"user_id": 42, "new_dept_id": 8},
            expires_at="2026-07-04T14:07:30Z",
            dry_run=DryRunSummary(summary="将影响 1 行", affected_count=1),
        )
        data = json.loads(event_to_sse_data(event))
        assert data["type"] == "confirmation_required"
        assert data["confirmationId"] == "conf_abc"
        assert data["expiresAt"] == "2026-07-04T14:07:30Z"
        assert data["dryRun"]["affectedCount"] == 1

    def test_confirmation_required_without_dry_run(self) -> None:
        """risk=high+count=None 或 destructive 直接 HITL，无 dryRun 字段"""
        event = ConfirmationRequiredEvent(
            confirmation_id="conf_x",
            tool="user.batch_delete",
            tool_call_id="tc_bd",
            summary="tool=user.batch_delete, risk=destructive, mode=hitl",
            args={"user_ids": [1, 2, 3]},
            expires_at="2026-07-04T14:07:30Z",
        )
        data = json.loads(event_to_sse_data(event))
        assert "dryRun" not in data  # None 字段剔除

    def test_ai_error(self) -> None:
        event = AiErrorEvent(error_code="LLM_API_ERROR", message="provider timeout")
        data = json.loads(event_to_sse_data(event))
        assert data["type"] == "ai_error"
        assert data["errorCode"] == "LLM_API_ERROR"

    def test_done_event(self) -> None:
        event = DoneEvent()
        data = json.loads(event_to_sse_data(event))
        assert data == {"type": "done"}

    def test_done_event_supports_not_applicable_persistence(self) -> None:
        event = DoneEvent(persistence="not_applicable", projection="unchanged")
        data = json.loads(event_to_sse_data(event))
        assert data == {
            "type": "done",
            "persistence": "not_applicable",
            "projection": "unchanged",
        }


class TestStringifyLargeInts:
    """spec §8.1 + CLAUDE.md #3: Snowflake ID（int64）序列化为 JSON 必须转 str

    JS Number.MAX_SAFE_INTEGER = 2^53 - 1 = 9007199254740991。
    Snowflake ID 是 int64，普遍 > 2^53（如 7483433649145122816）。
    若 JSON 序列化为 number，前端 JSON.parse 丢末尾精度（→ ...3000）。
    """

    def test_snowflake_id_in_args_stringified(self) -> None:
        """confirmation_required.args.user_ids 大 int → str"""
        event = ConfirmationRequiredEvent(
            confirmation_id="conf_x",
            tool="user.batch_delete",
            tool_call_id="tc_bd",
            summary="tool=user.batch_delete, risk=destructive, mode=hitl",
            args={"user_ids": [7483433649145122816, 7483433587736317952]},
            expires_at="2026-07-04T14:07:30Z",
        )
        data = json.loads(event_to_sse_data(event))
        assert data["args"]["user_ids"] == [
            "7483433649145122816",
            "7483433587736317952",
        ]

    def test_small_int_in_args_kept_as_int(self) -> None:
        """count / status 等小 int 保持 int 不变（避免无差别字符串化）"""
        event = _started(args={"count": 5, "status": 1, "user_id": 42})
        data = json.loads(event_to_sse_data(event))
        assert data["args"] == {"count": 5, "status": 1, "user_id": 42}

    def test_snowflake_id_in_result_stringified(self) -> None:
        """tool_call_result.result.user_ids 大 int → str（写操作返回影响 ID 列表）"""
        event = _result(
            result={
                "deleted": 2,
                "user_ids": [7483433649145122816, 7483433587736317952],
            },
        )
        data = json.loads(event_to_sse_data(event))
        assert data["result"]["deleted"] == 2  # 小 int 保留
        assert data["result"]["user_ids"] == [
            "7483433649145122816",
            "7483433587736317952",
        ]

    def test_nested_dict_snowflake_id_stringified(self) -> None:
        """嵌套 dict 中的 *_id 大 int 也要转"""
        event = _result(
            result={
                "affected": [{"user_id": 7483433649145122816}, {"user_id": 42}],
            },
        )
        data = json.loads(event_to_sse_data(event))
        assert data["result"]["affected"][0]["user_id"] == "7483433649145122816"
        assert data["result"]["affected"][1]["user_id"] == 42

    def test_bool_not_converted(self) -> None:
        """Python bool 是 int 子类，但 True/False 不应字符串化"""
        from app.modules.ai.agents.hitl.events import stringify_large_ints

        assert stringify_large_ints(True) is True
        assert stringify_large_ints(False) is False
        assert stringify_large_ints({"flag": True, "n": 7483433649145122816}) == {
            "flag": True,
            "n": "7483433649145122816",
        }

    def test_negative_large_int_stringified(self) -> None:
        """负数大 int 同样丢精度（理论不会出现，但 abs() 兜底）"""
        from app.modules.ai.agents.hitl.events import stringify_large_ints

        assert stringify_large_ints(-7483433649145122816) == "-7483433649145122816"

    def test_exact_boundary_2_pow_53_stringified(self) -> None:
        """2^53 本身也无法在 JS 精确表示（MAX_SAFE_INTEGER = 2^53 - 1）"""
        from app.modules.ai.agents.hitl.events import (
            JS_MAX_SAFE_INT,
            stringify_large_ints,
        )

        assert JS_MAX_SAFE_INT == 1 << 53
        assert stringify_large_ints(JS_MAX_SAFE_INT) == str(JS_MAX_SAFE_INT)
        assert stringify_large_ints(JS_MAX_SAFE_INT - 1) == JS_MAX_SAFE_INT - 1


class TestSseDataCompact:
    """spec §8.1: 自定义事件 JSON 序列化，None 字段剔除"""

    def test_none_omitted(self) -> None:
        """error_code/error_msg 是 None 时不应出现在 JSON"""
        event = _result(ok=True, result=None)
        sse = event_to_sse_data(event)
        assert "errorCode" not in sse
        assert "errorMsg" not in sse

    def test_nested_none_omitted(self) -> None:
        """嵌套 dict 内的 None 也要剔除"""
        event = _result(
            result={"count": 5, "items": None, "name": "test"},
        )
        data = json.loads(event_to_sse_data(event))
        assert "items" not in data["result"]
        assert data["result"]["count"] == 5
        assert data["result"]["name"] == "test"

    def test_false_zero_kept(self) -> None:
        """False / 0 / 空字符串不是 None，必须保留"""
        event = _result(
            ok=False,  # False 不是 None
            result=None,
            duration_ms=0,  # 0 不是 None
            error_code="ERR",
            error_msg="",
        )
        data = json.loads(event_to_sse_data(event))
        assert data["ok"] is False
        assert data["durationMs"] == 0
        assert data["errorMsg"] == ""


class TestToolResultToLlmString:
    """pydantic_ai_wrapper._tool_result_to_llm_string"""

    def test_success_dict(self) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
            _tool_result_to_llm_string,
        )

        result = ToolResult.success(data={"count": 5, "name": "test"})
        s = _tool_result_to_llm_string(result)
        assert '"count": 5' in s
        assert '"name": "test"' in s

    def test_success_list(self) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
            _tool_result_to_llm_string,
        )

        result = ToolResult.success(data=[1, 2, 3])
        s = _tool_result_to_llm_string(result)
        assert s == "[1, 2, 3]"

    def test_success_scalar(self) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
            _tool_result_to_llm_string,
        )

        result = ToolResult.success(data=42)
        s = _tool_result_to_llm_string(result)
        assert s == "42"

    def test_failure_format(self) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
            _tool_result_to_llm_string,
        )

        result = ToolResult.failure(
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="目标不在你的可见范围内",
        )
        s = _tool_result_to_llm_string(result)
        assert s == "[ToolError:AI_DATA_SCOPE_VIOLATION] 目标不在你的可见范围内"
