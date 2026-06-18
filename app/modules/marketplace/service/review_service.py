"""应用市场 - 审核记录 service（spec 13 + 14.3）。

Phase 1 只做规则检查（同步）+ 人工审核（手动 update）。
AI 审核 Phase 2 接入。

review 三阶段（spec 14.3）：
1. rule_check：版本提交时同步执行（VersionService.validate_manifest 已经覆盖一部分）
2. ai_review：Phase 2，调用 AI 风控服务（low/medium/high）
3. human_review：管理员手动通过/拒绝

Phase 1 跳过 AI：create_pending 时直接 ai_risk_level='skipped'。
"""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.marketplace.models import AppReview
from app.modules.marketplace.service.base import MarketplaceBaseService


class ReviewService(MarketplaceBaseService):
    """审核记录 service（spec 13 + 14.3）

    Phase 1 只做规则检查（同步） + 人工审核（手动 update）。
    AI 审核 Phase 2 加。
    """

    async def create_pending(
        self,
        db: AsyncSession,
        *,
        app_id: int,
        version_id: int,
        rule_check_result: dict[str, Any],
    ) -> AppReview:
        """版本提交时创建 pending 审核记录。

        Phase 1 跳过 AI 审核：ai_risk_level 直接写 'skipped'，
        final_status='pending' 等待人工审核。

        Args:
            db: 数据库会话（调用方负责 commit）
            app_id: 所属应用ID
            version_id: 被审核版本ID
            rule_check_result: 规则引擎检查结果（manifest 校验 + 其他静态规则）

        Returns:
            已 flush 拿到 id 的 AppReview 实例
        """
        review = AppReview(
            app_id=app_id,
            version_id=version_id,
            rule_check_result=rule_check_result,
            rule_check_at=datetime.utcnow(),
            human_status="pending",
            ai_risk_level="skipped",  # Phase 1 跳过 AI
            final_status="pending",
        )
        db.add(review)
        await db.flush()
        return review

    async def human_review(
        self,
        db: AsyncSession,
        *,
        review_id: int,
        reviewer_id: int,
        approved: bool,
        comment: str | None = None,
    ) -> AppReview:
        """人工审核：通过/拒绝 + 同步 final_status。

        Phase 1 没接入 AI，human_status 直接决定 final_status。
        Phase 2 接入 AI 后，final_status 需要综合 ai_risk_level 和 human_status。

        Args:
            db: 数据库会话
            review_id: 审核记录ID
            reviewer_id: 审核人ID（sys_user.user_id）
            approved: True=通过，False=拒绝
            comment: 审核意见（可选）

        Raises:
            NotFoundException: 审核记录不存在
        """
        review = await db.get(AppReview, review_id)
        if review is None:
            raise NotFoundException(
                resource_type="审核记录",
                error_code="APP_REVIEW_NOT_FOUND",
            )
        review.human_status = "approved" if approved else "rejected"
        review.human_reviewer_id = reviewer_id
        review.human_comment = comment
        review.human_reviewed_at = datetime.utcnow()
        review.final_status = "approved" if approved else "rejected"
        await db.flush()
        return review


review_service = ReviewService()
