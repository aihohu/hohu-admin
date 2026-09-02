"""Routing feedback aggregate query service.

与 routing_feedback_service.py（POST submit）分离：submit 是 append-only 写，
本 service 是复杂聚合查询，职责正交。
"""

from datetime import datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.schemas.routing_feedback import (
    FeedbackListItem,
    FeedbackSummary,
    TopCorrected,
    TopWrongAgent,
)
from app.modules.system.models.user import User


class RoutingFeedbackQueryService:
    async def summary(
        self, db: AsyncSession, days: int, *, tenant: TenantContext
    ) -> FeedbackSummary:
        """汇总 total、correct、wrong、wrongRate 和 topWrongAgents。

        决策 #21：topCorrected 并列时按 corrected_agent code ASC 取首.
        total=0 时 wrongRate=0，避免除零。
        """
        cutoff = datetime.now() - timedelta(days=days)

        base = select(AiRoutingFeedback).where(
            AiRoutingFeedback.tenant_id == tenant.tenant_id,
            AiRoutingFeedback.create_time >= cutoff,
        )

        # total / correct / wrong
        rows = (await db.execute(base)).scalars().all()
        total = len(rows)
        correct = sum(1 for r in rows if r.feedback == "correct")
        wrong = sum(1 for r in rows if r.feedback == "wrong")
        wrong_rate = round(wrong / total, 4) if total else 0.0

        # topWrongAgents：按 original_agent 聚合 wrong 数
        wrong_rows = [r for r in rows if r.feedback == "wrong"]
        wrong_by_agent: dict[str, list[AiRoutingFeedback]] = {}
        for r in wrong_rows:
            wrong_by_agent.setdefault(r.original_agent, []).append(r)

        # 取 name map（wrong agents + corrected agents）
        all_codes = set(wrong_by_agent.keys())
        corrected_codes = {r.corrected_agent for r in wrong_rows if r.corrected_agent}
        name_map: dict[str, str] = {}
        if all_codes or corrected_codes:
            name_rows = (
                await db.execute(
                    select(AiAgent.code, AiAgent.name).where(
                        AiAgent.code.in_(all_codes | corrected_codes)
                    )
                )
            ).all()
            name_map = {r[0]: r[1] for r in name_rows}

        top_wrong: list[TopWrongAgent] = []
        for code, items in wrong_by_agent.items():
            # topCorrected 众数，并列按 corrected_agent code ASC
            corrected_count: dict[str, int] = {}
            for it in items:
                if it.corrected_agent:
                    corrected_count[it.corrected_agent] = (
                        corrected_count.get(it.corrected_agent, 0) + 1
                    )
            top_corrected = None
            if corrected_count:
                # 排序：count desc, code asc
                sorted_corrected = sorted(
                    corrected_count.items(),
                    key=lambda kv: (-kv[1], kv[0]),
                )
                top_code, top_count = sorted_corrected[0]
                top_corrected = TopCorrected(
                    code=top_code,
                    name=name_map.get(top_code, top_code),
                    count=top_count,
                )
            top_wrong.append(
                TopWrongAgent(
                    agent_code=code,
                    agent_name=name_map.get(code, code),
                    wrong_count=len(items),
                    top_corrected=top_corrected,
                )
            )

        # 按 wrong_count desc, agent_code asc 排序，top 10
        top_wrong.sort(key=lambda x: (-x.wrong_count, x.agent_code))
        top_wrong = top_wrong[:10]

        return FeedbackSummary(
            days=days,
            total=total,
            correct=correct,
            wrong=wrong,
            wrong_rate=wrong_rate,
            top_wrong_agents=top_wrong,
        )

    async def list_items(
        self,
        db: AsyncSession,
        *,
        days: int,
        current: int,
        size: int,
        feedback: str,
        original_agent: str | None,
        corrected_agent: str | None,
        tenant: TenantContext,
    ) -> tuple[list[FeedbackListItem], int]:
        """分页查询反馈明细，feedback 支持 wrong 或 all，默认 wrong。"""
        cutoff = datetime.now() - timedelta(days=days)

        conditions = [
            AiRoutingFeedback.tenant_id == tenant.tenant_id,
            AiRoutingFeedback.create_time >= cutoff,
        ]
        if feedback != "all":
            conditions.append(AiRoutingFeedback.feedback == feedback)
        if original_agent:
            conditions.append(AiRoutingFeedback.original_agent == original_agent)
        if corrected_agent:
            conditions.append(AiRoutingFeedback.corrected_agent == corrected_agent)

        # join sys_user 取 user_name
        stmt = (
            select(AiRoutingFeedback, User.user_name)
            .outerjoin(
                User,
                (User.tenant_id == AiRoutingFeedback.tenant_id)
                & (User.user_id == AiRoutingFeedback.user_id),
            )
            .where(*conditions)
            .order_by(desc(AiRoutingFeedback.feedback_id))
            .offset((current - 1) * size)
            .limit(size)
        )
        rows = (await db.execute(stmt)).all()

        # 取所有 agent code（original + corrected）一次性查 name
        codes: set[str] = set()
        for r, _ in rows:
            codes.add(r.original_agent)
            if r.corrected_agent:
                codes.add(r.corrected_agent)
        agent_name_map: dict[str, str] = {}
        if codes:
            name_rows = (
                await db.execute(
                    select(AiAgent.code, AiAgent.name).where(AiAgent.code.in_(codes))
                )
            ).all()
            agent_name_map = {r[0]: r[1] for r in name_rows}

        items = [
            FeedbackListItem(
                feedback_id=r.feedback_id,
                message_id=r.message_id,
                user_id=r.user_id,
                user_name=username or "",
                original_agent=r.original_agent,
                original_agent_name=agent_name_map.get(
                    r.original_agent, r.original_agent
                ),
                feedback=r.feedback,
                corrected_agent=r.corrected_agent,
                corrected_agent_name=(
                    agent_name_map.get(r.corrected_agent, r.corrected_agent)
                    if r.corrected_agent
                    else None
                ),
                trace_id=r.trace_id,
                create_time=r.create_time,
            )
            for r, username in rows
        ]

        # total count
        count_stmt = (
            select(func.count()).select_from(AiRoutingFeedback).where(*conditions)
        )
        total = (await db.execute(count_stmt)).scalar() or 0
        return items, total


routing_feedback_query_service = RoutingFeedbackQueryService()
