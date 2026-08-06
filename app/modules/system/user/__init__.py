"""用户模块子包（v2.2 P1 #2.2 facade + 子模块拆分）。

Task 0a 阶段：仅建骨架，业务逻辑仍在 app.modules.system.service.user_service；
本子包通过 re-export 让新调用方使用 `from app.modules.system.user import user_service`，
旧调用方继续使用 `from app.modules.system.service import user_service`，零中断。

后续 Task（按 spec §2.2 迁移策略 A→D）会把业务逐步迁到本子包，
最终旧路径只保留 re-export 兼容。
"""

from app.modules.system.service.user_service import user_service

__all__ = ["user_service"]
