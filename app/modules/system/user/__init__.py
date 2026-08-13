"""用户模块 facade 与子模块入口。

业务实现仍兼容 ``app.modules.system.service.user_service``；
本子包通过 re-export 让新调用方使用 `from app.modules.system.user import user_service`，
旧调用方继续使用 `from app.modules.system.service import user_service`，零中断。

新调用方通过本子包导入，旧路径保留 re-export 兼容。
"""

from app.modules.system.service.user_service import user_service

__all__ = ["user_service"]
