"""HITL（Human-in-the-Loop）模块 — spec §8

组成：
  - constants.py: 状态机 enum（AiOperationStatus / AiExecutionMode / ConfirmAction）+ DryRunResult
  - risk.py: 风险分级判定（§5.3 autonomous vs HITL 矩阵）
  - manager.py: HITL Manager（Redis 挂起 + asyncio.Event 唤醒）

调用方：
  - Gateway Executor（Phase 3.2 接入）：调 classify_execution_mode 决定路径
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
