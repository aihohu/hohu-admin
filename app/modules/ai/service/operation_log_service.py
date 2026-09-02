"""AI 操作日志服务及状态机。

每次 tool 调用写一行 ai_operation_log，按 trace_id 串联同对话多 tool。
安全事件（注入命中 / Guardrail 命中）合并到 is_security_event 字段。

状态流转：
  autonomous:  start(running) → mark_success | mark_failed
  HITL:        start(pending_confirmation) → mark_running → mark_success | mark_failed
                pending_confirmation → mark_rejected（用户拒绝）
                pending_confirmation → mark_expired（5min TTL 超时 / 服务重启）

Gateway Executor 调用方式：
    log_id = await operation_log_service.start_operation(
        db, trace_id=..., tenant_id=..., tool_name=..., tool_call_id=..., risk_level=...,
        execution_mode=AiExecutionMode.HITL, status=AiOperationStatus.PENDING_CONFIRMATION,
        ...
    )
    # 挂起 / 唤醒后：
    await operation_log_service.mark_running(db, log_id)
    # 业务执行完：
    await operation_log_service.mark_success(db, log_id, result_summary=..., duration_ms=...)
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.constants import AiOperationStatus
from app.modules.ai.models.operation_log import AiOperationLog

_TARGET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TARGET_ID_RE = re.compile(r"^[1-9][0-9]*$")


def _now_not_before(*timestamps: datetime | None) -> datetime:
    """Return an application timestamp that preserves audit event ordering."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return max([now, *(timestamp for timestamp in timestamps if timestamp is not None)])


def build_target_summary(subject_refs: Any) -> str | None:
    """Serialize only canonical subject type/ID pairs for audit display."""
    if not isinstance(subject_refs, (list, tuple)) or not subject_refs:
        return None
    normalized: set[tuple[str, str]] = set()
    for ref in subject_refs:
        if not isinstance(ref, dict):
            return None
        subject_type = ref.get("type")
        subject_id = ref.get("id")
        if not isinstance(subject_type, str) or not _TARGET_TYPE_RE.fullmatch(
            subject_type
        ):
            return None
        if not isinstance(subject_id, str) or not _TARGET_ID_RE.fullmatch(subject_id):
            return None
        normalized.add((subject_type, subject_id))
    if not normalized:
        return None
    payload = [
        {"id": subject_id, "type": subject_type}
        for subject_type, subject_id in sorted(
            normalized,
            key=lambda item: (int(item[1]), item[0]),
        )
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


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
        tenant: TenantContext,
        source_user_message_id: int | None = None,
        readonly_snapshot: bool = False,
        agent_code: str | None = None,
        target_summary: str | None = None,
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
            tenant_id=tenant.tenant_id,
            source_user_message_id=source_user_message_id,
            readonly_snapshot=readonly_snapshot,
            agent_code=agent_code,
            target_summary=target_summary,
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
        # 修订 S-3：autonomous 流业务立即开始，写 started_at = queued_at（即 flush 后 server_default 填的值）
        # hitl_wait_ms = 0（无 HITL 等待）。HITL 流的 started_at 由 mark_running 填。
        if status == AiOperationStatus.RUNNING:
            log.started_at = log.queued_at
            log.hitl_wait_ms = 0
        return log.log_id

    async def mark_running(
        self, db: AsyncSession, log_id: int, *, tenant: TenantContext
    ) -> AiOperationLog:
        """状态迁移：pending_confirmation → running（用户 approve 后）

        修订 S-3：HITL 流真正进入"业务执行"状态，写 started_at + hitl_wait_ms。
        hitl_wait_ms = started_at - queued_at（pending 等待时间）。
        """
        log = await self._get(db, log_id, tenant=tenant)
        if log.status != AiOperationStatus.PENDING_CONFIRMATION.value:
            if AiOperationStatus(log.status).is_terminal:
                self._transition(log, AiOperationStatus.RUNNING)
            raise BusinessRuleException(
                f"ai_operation_log log_id={log_id} 已进入 running",
                error_code="AI_OPERATION_LOG_ALREADY_RUNNING",
            )
        now = _now_not_before(log.queued_at)
        # queued_at 由 server_default 填充；HITL 流 mark_running 时计算等待耗时
        if log.queued_at is not None:
            delta = (now - log.queued_at).total_seconds() * 1000
            hitl_wait_ms = max(0, int(delta))
        else:
            hitl_wait_ms = 0
        transitioned = await self._update_if_pending(
            db,
            log_id,
            status=AiOperationStatus.RUNNING.value,
            started_at=now,
            hitl_wait_ms=hitl_wait_ms,
            tenant=tenant,
        )
        if transitioned is not None:
            return transitioned
        await db.refresh(log)
        if AiOperationStatus(log.status).is_terminal:
            self._transition(log, AiOperationStatus.RUNNING)
        raise BusinessRuleException(
            f"ai_operation_log log_id={log_id} 已被其它执行者接管",
            error_code="AI_OPERATION_LOG_ALREADY_RUNNING",
        )

    async def mark_success(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        result_summary: str,
        duration_ms: int,
        target_summary: str | None = None,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """状态迁移：running → success（终态）"""
        log = await self._get(db, log_id, tenant=tenant)
        self._transition(log, AiOperationStatus.SUCCESS)
        log.result_summary = result_summary
        if target_summary is not None:
            log.target_summary = target_summary
        log.duration_ms = duration_ms
        log.finished_at = _now_not_before(log.queued_at, log.started_at)
        return log

    async def mark_failed(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        error_code: str,
        duration_ms: int,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """状态迁移：running → failed（终态）"""
        log = await self._get(db, log_id, tenant=tenant)
        self._transition(log, AiOperationStatus.FAILED)
        log.error_code = error_code
        log.duration_ms = duration_ms
        log.finished_at = _now_not_before(log.queued_at, log.started_at)
        return log

    async def mark_rejected(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        approved_by: int,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """状态迁移：pending_confirmation → rejected（终态，用户主动拒绝）

        ``approved_by`` 同时记录批准者或拒绝者。
        """
        log = await self._get(db, log_id, tenant=tenant)
        self._transition(log, AiOperationStatus.REJECTED)
        log.approved_by = approved_by
        log.finished_at = _now_not_before(log.queued_at, log.started_at)
        return log

    async def mark_rejected_if_pending(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        approved_by: int,
        tenant: TenantContext,
    ) -> AiOperationLog | None:
        """Reject only an orphaned pending operation; never overwrite a live run."""
        return await self._update_if_pending(
            db,
            log_id,
            status=AiOperationStatus.REJECTED.value,
            approved_by=approved_by,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
            tenant=tenant,
        )

    async def mark_expired(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        error_code: str | None = None,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """状态迁移：pending_confirmation → expired（终态）

        触发场景：5 分钟 TTL 超时或服务重启清扫。

        注意：此方法要求 log 当前状态必须是 pending_confirmation（其它状态
        会抛 AI_OPERATION_LOG_TERMINAL_STATE）。若调用方不确定当前状态
        （例如唤醒失败时日志可能已进入 running），
        请用幂等版本 `mark_expired_if_pending`。
        """
        log = await self._get(db, log_id, tenant=tenant)
        self._transition(log, AiOperationStatus.EXPIRED)
        log.error_code = error_code
        log.finished_at = _now_not_before(log.queued_at, log.started_at)
        return log

    async def mark_expired_if_pending(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        error_code: str | None = None,
        tenant: TenantContext,
    ) -> AiOperationLog | None:
        """幂等版本（修订 S-14 配套）：仅当 status=pending_confirmation 时迁移到 expired

        场景：wake 失败（stream_gone）时 log 可能已被并发路径 mark_running 或
        mark_approved + mark_running。仅 pending_confirmation 状态才需要标 expired。

        Returns:
            迁移后的 log（如果做了迁移）；None 表示当前状态非 pending_confirmation，
            调用方无需操作。
        """
        return await self._update_if_pending(
            db,
            log_id,
            status=AiOperationStatus.EXPIRED.value,
            error_code=error_code,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
            tenant=tenant,
        )

    async def attach_confirmation(
        self,
        db: AsyncSession,
        log_id: int,
        confirmation_id: str,
        *,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """回填 confirmation_id（HITL Manager 创建 pending 后调用）"""
        log = await self._get(db, log_id, tenant=tenant)
        if log.confirmation_id is not None:
            raise BusinessRuleException(
                "ai_operation_log.confirmation_id 已存在，不可重复设置",
                error_code="AI_OPERATION_LOG_CONFIRMATION_ALREADY_SET",
            )
        log.confirmation_id = confirmation_id
        return log

    async def attach_target_summary(
        self,
        db: AsyncSession,
        log_id: int,
        subject_refs: Any,
        *,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """Freeze an allowlisted target summary without exposing action arguments."""
        log = await self._get(db, log_id, tenant=tenant)
        summary = build_target_summary(subject_refs)
        if summary is not None:
            log.target_summary = summary
        return log

    async def mark_approved(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        approved_by: int,
        tenant: TenantContext,
    ) -> AiOperationLog:
        """记录 approved_by（status 不变，由 mark_running 单独迁移）

        设计：approved_by 与 status.running 是两个事实——
          - approved_by：谁按了 approve（审计追责）
          - status.running：业务真正开始执行（Gateway 拿到锁后迁移）

        ``/ai/confirm`` 用此方法记录 approved_by，Gateway Executor
        接到唤醒后调用 ``mark_running``。
        """
        log = await self._get(db, log_id, tenant=tenant)
        log.approved_by = approved_by
        return log

    async def get_by_tool_call_id(
        self,
        db: AsyncSession,
        tool_call_id: str,
        *,
        tenant: TenantContext,
        user_id: int | None = None,
    ) -> AiOperationLog | None:
        """按 tool_call_id 查询，供 SSE 断流后的兜底轮询使用。

        Args:
            user_id: 给定时做 owner 校验（不匹配抛 AI_OPERATION_LOG_FORBIDDEN）
            tenant_id: 必填可信租户作用域；跨租户与不存在同为 None
        """
        result = await db.execute(
            select(AiOperationLog).where(
                AiOperationLog.tool_call_id == tool_call_id,
                AiOperationLog.tenant_id == tenant.tenant_id,
            )
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

    async def _get(
        self, db: AsyncSession, log_id: int, *, tenant: TenantContext
    ) -> AiOperationLog:
        result = await db.execute(
            select(AiOperationLog).where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.log_id == log_id,
            )
        )
        log = result.scalars().first()
        if log is None:
            raise BusinessRuleException(
                f"ai_operation_log log_id={log_id} 不存在",
                error_code="AI_OPERATION_LOG_NOT_FOUND",
            )
        return log

    async def _update_if_pending(
        self,
        db: AsyncSession,
        log_id: int,
        *,
        tenant: TenantContext,
        **values,
    ) -> AiOperationLog | None:
        """CAS pending transition so wake/cleanup races cannot overwrite each other."""
        finished_at = values.get("finished_at")
        if isinstance(finished_at, datetime):
            terminal_floor = case(
                (
                    AiOperationLog.started_at.is_not(None),
                    AiOperationLog.started_at,
                ),
                else_=AiOperationLog.queued_at,
            )
            values["finished_at"] = case(
                (terminal_floor > finished_at, terminal_floor),
                else_=finished_at,
            )
        stmt = (
            update(AiOperationLog)
            .where(
                AiOperationLog.log_id == log_id,
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.status == AiOperationStatus.PENDING_CONFIRMATION.value,
            )
            .values(**values)
            .returning(AiOperationLog)
        )
        return (await db.execute(stmt)).scalars().one_or_none()

    def _transition(self, log: AiOperationLog, target: AiOperationStatus) -> None:
        """状态机迁移合法性校验 + 执行迁移

        规则：
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
