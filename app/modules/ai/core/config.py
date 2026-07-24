"""向后兼容层：旧代码 from app.modules.ai.core.config import ChatDeps

spec §17.2：ChatDeps 已迁移到 core/context.py 并扩展（含 user / perms /
data_scope / agent / trace_id 等字段）。本模块保留 re-export，1.5 重写
chat_agent / chat.py 时切换到完整新 ChatDeps。

新代码请直接 from app.modules.ai.core.context import ChatDeps。
"""

from app.modules.ai.core.context import ChatDeps  # noqa: F401
