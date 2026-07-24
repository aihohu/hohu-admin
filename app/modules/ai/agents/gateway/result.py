"""ToolResult — Gateway 执行结果标准化

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.5 / §9.6。

Gateway 内部所有 tool 执行都返回 ToolResult，统一异常转译：
  - 成功: ToolResult(ok=True, data=...)
  - 业务异常: ToolResult(ok=False, error_code=..., error_msg=...)
  - 不中断 SSE 流，LLM 看到 ok=false 会自然反问澄清
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Gateway tool 执行结果（统一容器）

    PydanticAI 包装层把 ToolResult 转成 LLM 可读的格式（如 JSON 字符串）返回给 LLM。
    API 层（如 /ai/confirm 端点）也可直接序列化 ToolResult 给前端。
    """

    ok: bool
    """True = 业务成功；False = 业务异常或鉴权拒绝"""

    data: Any = None
    """业务返回值（ok=True 时有效）"""

    error_code: str = ""
    """UPPER_SNAKE_CASE 错误码（ok=False 时必填），如 AI_DATA_SCOPE_VIOLATION"""

    error_msg: str = ""
    """给 LLM 看的错误描述（ok=False 时必填），LLM 据此反问用户"""

    meta: dict[str, Any] = field(default_factory=dict)
    """附加元信息（如 execution_mode / dry_run_count / duration_ms），不进 LLM context"""

    @classmethod
    def success(cls, data: Any, **meta: Any) -> "ToolResult":
        """构造成功结果"""
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error_code: str, error_msg: str, **meta: Any) -> "ToolResult":
        """构造失败结果

        Args:
            error_code: UPPER_SNAKE_CASE，前端 i18n 映射用（§9.6）
            error_msg: 给 LLM 的友好描述（如"目标用户不在你的可见范围"）
        """
        return cls(ok=False, error_code=error_code, error_msg=error_msg, meta=meta)
