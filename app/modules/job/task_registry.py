from collections.abc import Callable
from typing import Any

_task_functions: dict[str, Callable[..., Any]] = {}


def register_task(key: str):
    """装饰器：将函数注册为可调度的定时任务。

    Args:
        key: 任务唯一标识，对应 sys_job 表的 job_key 字段。

    Usage:
        @register_task("clean_logs")
        async def clean_logs(args: dict | None = None):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if key in _task_functions:
            msg = f"任务标识 '{key}' 已被注册"
            raise ValueError(msg)
        _task_functions[key] = func
        return func

    return decorator


def get_task_function(key: str) -> Callable[..., Any] | None:
    """根据 key 获取已注册的任务函数。"""
    return _task_functions.get(key)


def list_registered_tasks() -> list[dict[str, str]]:
    """返回所有已注册任务，供管理后台选择。"""
    return [
        {"key": k, "name": func.__doc__ or k} for k, func in _task_functions.items()
    ]
