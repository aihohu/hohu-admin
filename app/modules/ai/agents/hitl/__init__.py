"""HITL（Human-in-the-Loop）模块。

组成：
  - constants.py: 状态机 enum（AiOperationStatus / AiExecutionMode / ConfirmAction）+ DryRunResult
  - risk.py：autonomous 与 HITL 风险矩阵判定
  - manager.py: HITL Manager（Redis 挂起 + asyncio.Event 唤醒）

调用方：
  - Gateway Executor：调用 classify_execution_mode 决定执行路径
  - /ai/confirm endpoint：调 hitl_manager.wake 唤醒挂起的 SSE 流
  - main.py lifespan：调 cleanup_pending_on_startup 清扫 Redis 残留
"""

from .constants import (
    AI_OPERATION_LOG_DB_PREFIX,
    AiExecutionMode,
    AiOperationStatus,
    ConfirmAction,
    DryRunResult,
)

__all__ = [
    "AI_OPERATION_LOG_DB_PREFIX",
    "AiExecutionMode",
    "AiOperationStatus",
    "ConfirmAction",
    "DryRunResult",
]
