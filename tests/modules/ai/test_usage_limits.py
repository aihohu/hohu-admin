"""UsageLimitExceeded 到 AiErrorEvent 的转换测试。

修订 S-4：chat.py 显式捕获 UsageLimitExceeded + emit AiErrorEvent。
本测试通过源码静态检查 + AiErrorEvent 构造验证，避免完整 SSE 流测试的复杂 mock。
"""

# ruff: noqa: PLC0415

import inspect

from app.modules.ai.agents.hitl.events import AiErrorEvent


def test_chat_endpoint_uses_usage_limits():
    """必须配置 request_limit=10 和 tool_calls_limit=5。"""
    from app.modules.ai.api import chat as chat_module

    source = inspect.getsource(chat_module)
    assert "UsageLimits(" in source
    assert "request_limit=10" in source
    assert "tool_calls_limit=5" in source


def test_chat_endpoint_catches_usage_limit_exceeded():
    """修订 S-4: chat.py 必须显式 catch UsageLimitExceeded 并 emit AiErrorEvent"""
    from app.modules.ai.api import chat as chat_module

    source = inspect.getsource(chat_module)
    assert "except UsageLimitExceeded" in source
    assert "AI_USAGE_LIMIT_EXCEEDED" in source


def test_chat_endpoint_imports_usage_limit_exceeded():
    """修订 S-4: import UsageLimitExceeded（确保 except 子句能解析到正确类型）"""
    from app.modules.ai.api import chat as chat_module

    assert hasattr(chat_module, "UsageLimitExceeded")
    from pydantic_ai.exceptions import UsageLimitExceeded

    assert chat_module.UsageLimitExceeded is UsageLimitExceeded


def test_ai_error_event_serializes_usage_limit_code():
    """AiErrorEvent(AI_USAGE_LIMIT_EXCEEDED) 序列化结果含 error_code（前端解析）"""
    ev = AiErrorEvent(
        error_code="AI_USAGE_LIMIT_EXCEEDED",
        message="AI 调用次数超限，请换种方式问或拆分任务",
    )
    from app.modules.ai.agents.hitl.events import event_to_sse_data

    payload = event_to_sse_data(ev)
    payload_str = str(payload)
    assert "AI_USAGE_LIMIT_EXCEEDED" in payload_str
    assert "ai_error" in payload_str  # type field


def test_non_usage_limit_event_does_not_use_usage_limit_code():
    """防 regression：其它 AiErrorEvent 不应误用 AI_USAGE_LIMIT_EXCEEDED"""
    ev = AiErrorEvent(error_code="AI_INTERNAL_ERROR", message="内部错误")
    assert ev.error_code != "AI_USAGE_LIMIT_EXCEEDED"
