"""AI 操作日志服务 — spec §9.1 / §4.4 状态机

每次 tool 调用写一行 ai_operation_log，按 trace_id 串联同对话多 tool。
安全事件（注入命中 / Guardrail 命中）合并到 is_security_event 字段。

状态流转（§4.4 / §8.3）：
  autonomous:  start(running) → mark_success | mark_failed
  HITL:        start(pending_confirmation) → mark_running → mark_success | mark_failed
                pending_confirmation → mark_rejected（用户拒绝）
                pending_confirmation → mark_expired（5min TTL 超时 / 服务重启）

调用方（Phase 3.2 Gateway Executor 接入）：
    log_id = await operation_log_service.start_operation(
        db, trace_id=..., tool_name=..., tool_call_id=..., risk_level=...,
        execution_mode=AiExecutionMode.HITL, status=AiOperationStatus.PENDING_CONFIRMATION,
        ...
    )
    # 挂起 / 唤醒后：
    await operation_log_service.mark_running(db, log_id)
    # 业务执行完：
    await operation_log_service.mark_success(db, log_id, result_summary=..., duration_ms=...)
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.hitl.constants import AiOperationStatus
from app.modules.ai.models.operation_log import AiOperationLog


class OperationLogService:
    """ai_operation_log 表的写入 + 状态机迁移

    Service 不 commit（CLAUDE.md 规则 7），调用方负责。
    """

    async def start_operation(
        self,
        db: AsyncSession,
        *,
        trace_id: str,
        conversation_id: int,
        user_id: int,
        tool_name: str,
        tool_call_id: str,
        args_hash: str,
        args_summary: str,
        risk_level: str,
        execution_mode: str,
        status: AiOperationStatus,
        confirmation_id: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        is_security_event: bool = False,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> int:
        """写入新行，返回 log_id（caller 负责 commit）

        Args:
            status: 初始状态 — autonomous 流走 RUNNING，HITL 流走 PENDING_CONFIRMATION
            confirmation_id: HITL 流必填，autonomous 流 None
            is_security_event / event_type / severity: 安全事件合并字段
                （injection_pattern_matched / guardrail_keyword 等）

        Returns:
            log_id（Snowflake，BIGINT）
        """
        log = AiOperationLog(
            trace_id=trace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args_hash=args_hash,
            args_summary=args_summary,
            risk_level=risk_level,
            execution_mode=execution_mode,
            status=status.value,
            confirmation_id=confirmation_id,
            ip=ip,
            user_agent=user_agent,
            is_security_event=is_security_event,
            event_type=event_type,
            severity=severity,
        )
        db.add(log)
        await db.flush()
        return log.log_id

    async def mark_running(self, db: AsyncSession, log_id: int) -> AiOperationLog:
        """状态迁移：pending_confirmation → running（用户 approve 后）"""
        log = await self._get(db, log_id)
        self._transition(log, AiOperationStatus.RUNNING)
        return log

    async def mark_success(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        result_summary: str,
        duration_ms: int,
    ) -> AiOperationLog:
        """状态迁移：running → success（终态）"""
        log = await self._get(db, log_id)
        self._transition(log, AiOperationStatus.SUCCESS)
        log.result_summary = result_summary
        log.duration_ms = duration_ms
        log.finished_at = datetime.now(UTC).replace(tzinfo=None)
        return log

    async def mark_failed(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        error_code: str,
        duration_ms: int,
    ) -> AiOperationLog:
        """状态迁移：running → failed（终态）"""
        log = await self._get(db, log_id)
        self._transition(log, AiOperationStatus.FAILED)
        log.error_code = error_code
        log.duration_ms = duration_ms
        log.finished_at = datetime.now(UTC).replace(tzinfo=None)
        return log

    async def mark_rejected(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        approved_by: int,
    ) -> AiOperationLog:
        """状态迁移：pending_confirmation → rejected（终态，用户主动拒绝）

        approved_by 字段记录"拒绝者"，与 approved 的 approved_by 共用字段（§4.4）。
        """
        log = await self._get(db, log_id)
        self._transition(log, AiOperationStatus.REJECTED)
        log.approved_by = approved_by
        log.finished_at = datetime.now(UTC).replace(tzinfo=None)
        return log

    async def mark_expired(self, db: AsyncSession, log_id: int) -> AiOperationLog:
        """状态迁移：pending_confirmation → expired（终态）

        触发场景：5min TTL 超时 / 服务重启清扫（spec §8.4）
        """
        log = await self._get(db, log_id)
        self._transition(log, AiOperationStatus.EXPIRED)
        log.finished_at = datetime.now(UTC).replace(tzinfo=None)
        return log

    async def attach_confirmation(
        self,
        db: AsyncSession,
        log_id: int,
        confirmation_id: str,
    ) -> AiOperationLog:
        """回填 confirmation_id（HITL Manager 创建 pending 后调用）"""
        log = await self._get(db, log_id)
        if log.confirmation_id is not None:
            raise BusinessRuleException(
                "ai_operation_log.confirmation_id 已存在，不可重复设置",
                error_code="AI_OPERATION_LOG_CONFIRMATION_ALREADY_SET",
            )
        log.confirmation_id = confirmation_id
        return log

    async def mark_approved(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        approved_by: int,
    ) -> AiOperationLog:
        """记录 approved_by（status 不变，由 mark_running 单独迁移）

        设计：approved_by 与 status.running 是两个事实——
          - approved_by：谁按了 approve（审计追责）
          - status.running：业务真正开始执行（Gateway 拿到锁后迁移）

        Phase 3.1 /ai/confirm endpoint 用此方法记 approved_by，
        Phase 3.2 Gateway Executor 接到唤醒后调 mark_running。
        """
        log = await self._get(db, log_id)
        log.approved_by = approved_by
        return log

    async def get_by_tool_call_id(
        self,
        db: AsyncSession,
        tool_call_id: str,
        *,
        user_id: int | None = None,
    ) -> AiOperationLog | None:
        """按 tool_call_id 查（§9.3 SSE 断流兜底轮询用）

        Args:
            user_id: 给定时做 owner 校验（不匹配抛 AI_OPERATION_LOG_FORBIDDEN）
        """
        result = await db.execute(
            select(AiOperationLog).where(AiOperationLog.tool_call_id == tool_call_id)
        )
        log = result.scalars().first()
        if log is None:
            return None
        if user_id is not None and log.user_id != user_id:
            raise BusinessRuleException(
                "无权查询此 AI 操作日志",
                error_code="AI_OPERATION_LOG_FORBIDDEN",
            )
        return log

    async def _get(self, db: AsyncSession, log_id: int) -> AiOperationLog:
        result = await db.execute(
            select(AiOperationLog).where(AiOperationLog.log_id == log_id)
        )
        log = result.scalars().first()
        if log is None:
            raise BusinessRuleException(
                f"ai_operation_log log_id={log_id} 不存在",
                error_code="AI_OPERATION_LOG_NOT_FOUND",
            )
        return log

    def _transition(self, log: AiOperationLog, target: AiOperationStatus) -> None:
        """状态机迁移合法性校验 + 执行迁移

        规则（spec §4.4）：
          - 已终态（success/failed/rejected/expired）不可再迁移
          - 其它状态可迁到任意非终态（pending_confirmation ↔ running 允许双向？不允许：只能向前）
        """
        current = AiOperationStatus(log.status)
        if current.is_terminal:
            raise BusinessRuleException(
                f"ai_operation_log log_id={log.log_id} 已终态 {current.value}，"
                f"不可迁移到 {target.value}",
                error_code="AI_OPERATION_LOG_TERMINAL_STATE",
            )
        log.status = target.value


operation_log_service = OperationLogService()
