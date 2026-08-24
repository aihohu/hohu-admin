"""AI 模块共享常量.

抽出独立文件避免 service ↔ agents.supervisor 循环 import.
"""

DEFAULT_AGENT_CODE = "user_mgmt"
"""粘滞失效、Supervisor 关闭、无 Provider 或 legacy_null_mode
的终极 fallback。"""

AI_CHAT_USE_PERMISSION = "ai:chat:use"
"""AI 用户入口权限；R_SUPER 也必须通过 seed 显式获得。"""

AI_FILE_PARSE_PERMISSION = "ai:file:parse"
"""默认关闭的文件解析 Tool 所需显式权限。"""

AI_AGENT_EDIT_PERMISSION = "ai:agent:edit"
"""Agent 配置修改权限；必须与启用的 R_SUPER 身份同时满足。"""

PUBLISHED_AGENT_CODES = {"shared", "user_mgmt", "dept_mgmt", "role_mgmt"}
"""当前阶段已完成纵向切片、fresh/upgrade 新行允许默认启用的 Agent。

The three MVP business Agents join this set only after the Phase 2/3 write and
delegation slices are complete. Existing deployment-controlled enabled values
remain untouched by upgrade seeds.
"""

PUBLISHED_AGENT_TOOL_PERMISSIONS = frozenset(
    {
        "system:dept:add",
        "system:dept:edit",
        "system:dept:list",
        "system:dept:move",
        "system:role:add",
        "system:role:ai-agent-auth",
        "system:role:edit",
        "system:role:list",
        "system:role:menu-auth",
        "system:user:add",
        "system:user:delete",
        "system:user:edit",
        "system:user:export",
        "system:user:import",
        "system:user:list",
        "system:user:reset-password",
        "system:user:role-auth",
    }
)
"""Explicit Tool permissions required by the currently published business Agents."""
