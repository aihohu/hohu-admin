"""AiToolMeta 与常量定义。

AiToolMeta 是 tool 的"声明式元数据"，与业务函数同源不漂移：
- 鉴权（risk / required_perms / hitl_always）
- 脱敏（sensitive_input / sensitive_output）
- 聚合专用（readonly / allowed_filters / allowed_group_by / max_groups）
- LLM 友好（summary / ambiguous_without）

frozen dataclass 保证运行时不可变，装饰器注册后字段值不会被业务方误改。
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AiToolMeta:
    """Tool 元数据，由 @ai_tool 装饰器消费。

    字段分组：
      基础（必填）：name / agent / summary / required_perms / risk
      鉴权控制：hitl_always / dry_run_supported / idempotent
      脱敏：sensitive_input / sensitive_output
      LLM 交互：ambiguous_without / accepts_file / produces_file
      聚合专用：readonly / allowed_filters / allowed_group_by / max_groups
    """

    # ============ 基础（必填） ============
    name: str
    """Tool 全局唯一全限定名，如 'user.create' / 'user.stats'"""

    agent: str
    """所属 Agent code，启动时校验其在 ai_agent 表中存在。"""

    summary: str
    """给 LLM 的 1 句描述，≤ 100 Unicode chars（lint 校验）"""

    required_perms: tuple[str, ...]
    """权限码，复数同时满足（⊆ user.perms）。每个 perm 必须在 sys_menu 表存在（启动校验）"""

    risk: Literal["low", "high", "destructive"]
    """风险等级。仅允许 low、high、destructive，无 medium。"""

    # ============ 鉴权控制 ============
    hitl_always: bool = False
    """True 时强制走 HITL，无视 risk + dry_run_count"""

    dry_run_supported: bool = False
    """True 时同模块必须按约定提供 _dry_run_<tool>。"""

    idempotent: bool = False
    """是否可安全重放。未知默认 False；仅纯读或有稳定幂等键的 tool 可声明 True"""

    super_admin_only: bool = False
    """True 时仅超管可调，非超管在 dry_run 和风险分级前短路
    返回 AI_SUPER_ADMIN_REQUIRED（不走 HITL，不进入 dry_run / 风险分级）。
    典型场景：改权限码 / 改 R_SUPER 角色绑定 / 删除 super_admin 账号。"""

    # ============ 脱敏 ============
    sensitive_input: tuple[str, ...] = field(default_factory=tuple)
    """由可信后端策略生成且不会进入模型可见函数签名的字段。"""

    sensitive_output: tuple[str, ...] = field(default_factory=tuple)
    """永不回显给模型的返回字段列表。"""

    # ============ LLM 交互 ============
    ambiguous_without: tuple[str, ...] = field(default_factory=tuple)
    """缺少这些字段时模型应向用户澄清。"""

    accepts_file: tuple[str, ...] = field(default_factory=tuple)
    """工具接受的文件 MIME 类型。"""

    produces_file: bool = False
    """工具是否产生可下载文件。"""

    interaction_flow: Literal["direct", "prepared"] = "direct"
    """确认拓扑。prepared tool 成功后由 Gateway 根据 requested_outcome 编排。"""

    prepared_execute_tool: str | None = None
    """prepared preview 绑定的 Gateway-only execute tool 全限定名。"""

    llm_visible: bool = True
    """False 表示仅 Gateway 可调用，不进入模型可见 tool 集合。"""

    # ============ 聚合工具专用 ============
    readonly: bool = False
    """True 表示纯读无副作用。"""

    allowed_filters: tuple[str, ...] = field(default_factory=tuple)
    """filters dict 允许的 key 白名单（防 LLM 查 password_hash 等敏感字段）"""

    allowed_group_by: tuple[str, ...] = field(default_factory=tuple)
    """group_by 允许的字段白名单（防按 phone / email 等高基数字段 group）"""

    max_groups: int = 20
    """group_by 返回组数上限，超限截断"""

    # ============ Tool Result View ============
    result_view: str = "plain_json"
    """标准 view_type key，决定前端按哪个组件渲染结果。
    必须在 STANDARD_VIEW_TYPES 内（启动校验）：
      - rows_affected: 写操作影响行数（user.batch_delete）
      - data_list: 列表查询（role.list / dept.list）
      - stats_chart: 统计图表（user.stats）
      - detail_card: 单实体详情（job.update_cron 返回值）
      - plain_json: fallback（user.count 这种纯数字 + chip 跳转）
    默认使用 plain_json 兼容未声明结构化视图的工具。"""

    chip_target: str | None = None
    """结果卡跳转目标路径（如 '/system/user'）。
    readonly tool 声明 chip_target 后：
      - 后端写 ai:query_cache hash 时 module 字段填此路径
      - 前端 tool_call_started 事件携带 chipTarget，不再硬编码 CHIP_TARGETS map
    None = 不显示 chip（写 tool / stats tool / detail tool 都不需要 chip）。
    替代旧字段 query_cache_module（保留为 alias，新代码用 chip_target）。"""

    # ============ 结果卡跳转 ============
    query_cache_module: str | None = None
    """readonly tool 的查询条件写入 ai:query_cache:<trace_id> hash 时的 module 字段，
    用于前端 chip 跳转回放筛选（如 "system/user"）。
    None = 不写 query_cache（非 readonly / 不需 chip 跳转的 tool）。"""

    # ============ 可见性 ============
    default_enabled: bool = True
    """True=默认启用（受 perms 过滤后可见），False=默认禁用，
    仅当 tool.name 在 sys_config.ai:enabled_tools JSON 数组中时才启用。
    典型场景：file.parse / provider.export 等高风险 tool 默认 False，
    部署方评估后显式加入 ai:enabled_tools 启用。"""

    # ============ 审计 ============
    args_summary_fields: tuple[str, ...] = field(default_factory=tuple)
    """声明要写入 ai_operation_log.args_summary 的 args 字段名（白名单）。
    默认空 tuple → summary 仅含元信息（MVP 行为，向后兼容）。
    字段名必须不在 SENSITIVE_INPUT_BLOCKLIST 内（check_ai_tools.py 静态校验）。
    典型场景：user.update_dept 声明 ('user_id', 'new_dept_id') 让审计直接看到关键参数。"""


# ============ 常量 ============

# shared Agent 对所有登录用户可见，不要求角色绑定。
# - 任何登录用户直通（不需要 role_ai_agent 绑定）
# - file.parse 等通用 tool 归属它
SHARED_AGENT_CODE = "shared"

# 工具参数命中此黑名单时，Lint 强制要求声明 sensitive_input。
# 命中但未声明 → 阻断合并（scripts/check_ai_tools.py 的 blocklist_field_must_be_sensitive）
SENSITIVE_INPUT_BLOCKLIST = (
    "password",
    "password_hash",
    "salt",
    "api_key",
    "secret_key",
    "private_key",
    "access_token",
    "refresh_token",
    "session_token",
    "secret",
    "token",
)

# 标准 view_type 集合；启动时校验工具声明。
STANDARD_VIEW_TYPES: frozenset[str] = frozenset(
    {
        "rows_affected",
        "data_list",
        "stats_chart",
        "detail_card",
        "plain_json",
    }
)
