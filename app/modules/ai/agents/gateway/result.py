"""ToolResult / UIResult — Gateway 执行结果标准化

区分供 LLM 使用的结果和供前端渲染的 UI 结果，避免展示数据进入模型上下文。

双层设计（决策 3 修正：ui 可选，lint 强制 builtin tool 填 ui）：
  ToolResult.data — 给 LLM（精简，进 prompt cache，serialize_for_llm 脱敏）
  ToolResult.ui  — 给前端（丰富，不进 LLM context）

内置工具必须填写 ``ui``，并由 lint 强制检查：
  return ToolResult.success(
      data={"deleted": 2},
      ui=UIResult(view_type="rows_affected", view_data={"count": 2, "ids": [...]}),
  )

executor 兼容路径（dict / list 返回值，第三方 tool 或老代码）：
  return ToolResult.success(data=safe_data)  # ui=None，前端 fallback plain_json
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UIResult:
    """供前端按 ``view_type`` 路由组件的 UI 结果，不进入 LLM prompt。

    view_data 不强校验 schema（决策 10）：后端保持 dict[str, Any] 灵活，
    前端 TS discriminated union 给类型安全。业务方写错只影响自家 tool 渲染。
    """

    view_type: str
    """标准 view_type key（启动校验，必须在 STANDARD_VIEW_TYPES）"""

    view_data: dict[str, Any]
    """view_type 对应组件的 props（如 rows_affected 的 {count, ids}）"""

    audit: dict[str, Any] = field(default_factory=dict)
    """标准化审计字段（affected_user_ids / before_value / after_value 等），
    写入 ai_operation_log.result_summary 供后台审计页反查。决策 4 注：与
    args_summary_fields 正交——前者审计入参，后者审计结果。"""

    label_key: str = ""
    """i18n key（如 ai.tool.user.batch_delete.result）；空字符串表示用默认文案"""

    label_params: dict[str, Any] = field(default_factory=dict)
    """i18n 插值参数（如 {"count": 2} → "已删除 {count} 行"）"""


@dataclass(frozen=True)
class PreparedActionProposal:
    """prepared tool 给 Gateway 的内部执行提案，不进入 LLM/SSE。"""

    frozen_args: dict[str, Any]
    """批准后调用绑定 execute tool 的服务端冻结参数。"""

    snapshot: dict[str, Any]
    """预检时生成、批准前复验的业务快照。"""

    subject_ref: dict[str, str]
    """业务对象的不透明引用，例如 user_import_batch。"""

    presentation: dict[str, Any]
    """允许确认 UI 展示的安全摘要；不得含 token/raw args。"""

    expires_at: datetime
    """提案失效时间。"""

    snapshot_hash: str = ""
    """业务快照的 canonical hash，批准前必须复验。"""


@dataclass
class ToolResult:
    """Gateway tool 执行结果（统一容器）

    PydanticAI 包装层把 ToolResult.data 转成 LLM 可读的格式返回给 LLM。
    API 层（如 /ai/confirm 端点）也可直接序列化 ToolResult 给前端。
    """

    ok: bool
    """True = 业务成功；False = 业务异常或鉴权拒绝"""

    data: Any = None
    """业务返回值（ok=True 时有效）— 给 LLM 看，serialize_for_llm 脱敏后进 prompt"""

    ui: UIResult | None = None
    """供前端使用且不进入 LLM context 的 UI 结果。
    None（ok=False / executor 兼容路径 / 业务方未声明）→ 前端 fallback plain_json。
    内置工具由 lint 强制要求提供 ``ui``。"""

    prepared_action: PreparedActionProposal | None = None
    """prepared tool 的内部提案；序列化层只读取 data/ui，不得向模型或客户端暴露。"""

    error_code: str = ""
    """UPPER_SNAKE_CASE 错误码（ok=False 时必填），如 AI_DATA_SCOPE_VIOLATION"""

    error_msg: str = ""
    """给 LLM 看的错误描述（ok=False 时必填），LLM 据此反问用户"""

    meta: dict[str, Any] = field(default_factory=dict)
    """执行元信息（duration_ms / execution_mode / retry_count），不进 SSE，仅日志 / metric"""

    @classmethod
    def success(
        cls,
        data: Any,
        *,
        ui: UIResult | None = None,
        prepared_action: PreparedActionProposal | None = None,
        **meta: Any,
    ) -> "ToolResult":
        """构造成功结果

        Args:
            data: 给 LLM 的精简数据（进 prompt cache）
            ui: 给前端 UI 的丰富数据（不进 prompt）；None 时前端 fallback plain_json
            **meta: 执行元信息（duration_ms 等，不进 SSE）
        """
        return cls(
            ok=True,
            data=data,
            ui=ui,
            prepared_action=prepared_action,
            meta=meta,
        )

    @classmethod
    def failure(cls, error_code: str, error_msg: str, **meta: Any) -> "ToolResult":
        """构造失败结果

        Args:
            error_code: UPPER_SNAKE_CASE，供前端 i18n 映射
            error_msg: 给 LLM 的友好描述（如"目标用户不在你的可见范围"）
        """
        return cls(ok=False, error_code=error_code, error_msg=error_msg, meta=meta)
