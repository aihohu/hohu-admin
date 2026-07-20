"""AiToolMeta 与常量定义

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.1 / §5.5 / §7.2。

AiToolMeta 是 tool 的"声明式元数据"，与业务函数同源不漂移：
- 鉴权（risk / required_perms / hitl_always）
- 脱敏（sensitive_input / sensitive_output）
- 聚合专用（readonly / allowed_filters / allowed_group_by / max_groups，§5.5）
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
      聚合专用（§5.5，默认值不影响普通 tool）：readonly / allowed_filters / allowed_group_by / max_groups
    """

    # ============ 基础（必填） ============
    name: str
    """Tool 全局唯一全限定名，如 'user.create' / 'user.stats'"""

    agent: str
    """所属 Agent code，必须在 ai_agent 表中存在（启动校验，spec §10.1）"""

    summary: str
    """给 LLM 的 1 句描述，≤ 100 Unicode chars（lint 校验）"""

    required_perms: tuple[str, ...]
    """权限码，复数同时满足（⊆ user.perms）。每个 perm 必须在 sys_menu 表存在（启动校验）"""

    risk: Literal["low", "high", "destructive"]
    """风险等级，§5.3 风险分级判定用。注意：只有 3 档，无 'medium'"""

    # ============ 鉴权控制 ============
    hitl_always: bool = False
    """True 时强制走 HITL，无视 risk + dry_run_count"""

    dry_run_supported: bool = False
    """True 时同模块必须有 _dry_run_<tool>（命名约定，spec §5.1）"""

    idempotent: bool = True
    """是否幂等（重复调用安全）。非幂等 tool 在 LLM 重试场景需特殊处理"""

    super_admin_only: bool = False
    """§11.2 super_admin gate：True 时仅超管（is_super_admin）可调，非超管短路
    返回 AI_SUPER_ADMIN_REQUIRED（不走 HITL，不进入 dry_run / 风险分级）。
    典型场景：改权限码 / 改 R_SUPER 角色绑定 / 删除 super_admin 账号。"""

    # ============ 脱敏 ============
    sensitive_input: tuple[str, ...] = field(default_factory=tuple)
    """字段名列表。声明但**不进函数签名**（spec §7.2），由后端策略生成"""

    sensitive_output: tuple[str, ...] = field(default_factory=tuple)
    """返回字段列表，永不回显给 LLM（spec §7.3）"""

    # ============ LLM 交互 ============
    ambiguous_without: tuple[str, ...] = field(default_factory=tuple)
    """缺这些字段时 LLM 应主动反问（spec §8.6 MISSING_ARGUMENT）"""

    accepts_file: tuple[str, ...] = field(default_factory=tuple)
    """接受的 MIME 类型（spec §16 文件上传场景）"""

    produces_file: bool = False
    """是否产生文件下载（spec §16.2 同步导出）"""

    # ============ 聚合 tool 专用（§5.5，默认值不影响普通 tool） ============
    readonly: bool = False
    """True = 纯读无副作用，被 §2.9 chip 机制忽略（聚合结果即答案）"""

    allowed_filters: tuple[str, ...] = field(default_factory=tuple)
    """filters dict 允许的 key 白名单（防 LLM 查 password_hash 等敏感字段）"""

    allowed_group_by: tuple[str, ...] = field(default_factory=tuple)
    """group_by 允许的字段白名单（防按 phone / email 等高基数字段 group）"""

    max_groups: int = 20
    """group_by 返回组数上限，超限截断"""

    # ============ chip 跳转（§2.9 / §8.7） ============
    query_cache_module: str | None = None
    """readonly tool 的查询条件写入 ai:query_cache:<trace_id> hash 时的 module 字段，
    用于前端 chip 跳转回放筛选（如 "system/user"）。
    None = 不写 query_cache（非 readonly / 不需 chip 跳转的 tool）。"""

    # ============ 可见性（v1.5+ SR-17） ============
    default_enabled: bool = True
    """True=默认启用（受 perms 过滤后可见），False=默认禁用，
    仅当 tool.name 在 sys_config.ai:enabled_tools JSON 数组中时才启用。
    典型场景：file.parse / provider.export 等高风险 tool 默认 False，
    部署方评估后显式加入 ai:enabled_tools 启用。"""


# ============ 常量 ============

# spec §5.4: shared Agent code 是特殊值
# - 任何登录用户直通（不需要 role_ai_agent 绑定）
# - file.parse 等通用 tool 归属它
SHARED_AGENT_CODE = "shared"

# spec §7.2: sensitive_input 字段命中此黑名单时，Lint 强制要求声明 sensitive_input
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
