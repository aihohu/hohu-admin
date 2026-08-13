"""AI 模块共享常量.

抽出独立文件避免 service ↔ agents.supervisor 循环 import.
"""

DEFAULT_AGENT_CODE = "user_mgmt"
"""粘滞失效、Supervisor 关闭、无 Provider 或 legacy_null_mode
的终极 fallback。"""
