"""任务函数包 — 导入所有任务模块以触发装饰器注册。"""

from app.modules.job.tasks import log_tasks

__all__ = ["log_tasks"]
