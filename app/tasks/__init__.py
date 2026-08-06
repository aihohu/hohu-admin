"""Task functions — import all task modules to trigger @register_task registration."""

from app.tasks import log_tasks, user_cleanup_tasks

__all__ = ["log_tasks", "user_cleanup_tasks"]
