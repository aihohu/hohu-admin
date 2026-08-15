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

PUBLISHED_AGENT_CODES = {"shared"}
"""当前阶段已完成纵向切片、fresh/upgrade 新行允许默认启用的 Agent。

``user_mgmt``、``dept_mgmt`` 和 ``role_mgmt`` 必须分别等待 Phase 2/3
补齐基线要求的写入与委派能力后再加入，避免中间构建提前宣称可发布。
"""
