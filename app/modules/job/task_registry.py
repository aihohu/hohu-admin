from collections.abc import Callable
from enum import StrEnum
from typing import Any

_task_functions: dict[str, Callable[..., Any]] = {}
_task_scopes: dict[str, "TaskScope"] = {}


class TaskScope(StrEnum):
    """Authority boundary used to decide which scheduler may invoke a task."""

    TENANT = "tenant"
    PLATFORM = "platform"


def register_task(key: str, *, scope: TaskScope = TaskScope.TENANT):
    """装饰器：将函数注册为可调度的定时任务。

    Args:
        key: 任务唯一标识，对应 sys_job 表的 job_key 字段。

    Usage:
        @register_task("clean_logs")
        async def clean_logs(args: dict | None = None):
            ...
    """

    if not isinstance(scope, TaskScope):
        raise TypeError("scope must be a TaskScope")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if key in _task_functions:
            msg = f"任务标识 '{key}' 已被注册"
            raise ValueError(msg)
        _task_functions[key] = func
        _task_scopes[key] = scope
        return func

    return decorator


def get_task_function(key: str) -> Callable[..., Any] | None:
    """根据 key 获取已注册的任务函数。"""
    return _task_functions.get(key)


def get_task_scope(key: str) -> TaskScope | None:
    """Return the declared task scope; direct test registrations stay tenant-local."""
    if key not in _task_functions:
        return None
    return _task_scopes.get(key, TaskScope.TENANT)


def is_tenant_task(key: str) -> bool:
    """Fail closed unless a registered task belongs to a tenant scheduler."""
    return get_task_scope(key) == TaskScope.TENANT


def tenant_task_keys() -> tuple[str, ...]:
    """Return the stable tenant-visible registry key set."""
    return tuple(sorted(key for key in _task_functions if is_tenant_task(key)))


def list_registered_tasks(*, scope: TaskScope | None = None) -> list[dict[str, str]]:
    """返回所有已注册任务，供管理后台选择。"""
    return [
        {"key": key, "name": func.__doc__ or key}
        for key, func in _task_functions.items()
        if scope is None or get_task_scope(key) == scope
    ]
