"""[CLOUD-ONLY] AppReview 表 service

部署在云市场。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：应用市场 - 审核记录 service（spec 13 + 14.3）。

Phase 1 只做规则检查（同步）+ 人工审核（手动 update）。
AI 审核 Phase 2 接入。

review 三阶段（spec 14.3）：
1. rule_check：版本提交时同步执行（VersionService.validate_manifest 已经覆盖一部分）
2. ai_review：Phase 2，调用 AI 风控服务（low/medium/high）
3. human_review：管理员手动通过/拒绝

Phase 1 跳过 AI：create_pending 时直接 ai_risk_level='skipped'。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.marketplace.models import App, AppReview, AppVersion
from app.modules.marketplace.service.base import MarketplaceBaseService


class ReviewService(MarketplaceBaseService):
    """审核记录 service（spec 13 + 14.3）

    Phase 1 只做规则检查（同步） + 人工审核（手动 update）。
    AI 审核 Phase 2 加。

    注意：AppReview 表无 tenant_id（通过 app_id FK 隐式继承），
    因此 list/get 不走 self.scoped()，直接 join App + AppVersion。
    """

    async def list_reviews(
        self,
        db: AsyncSession,
        *,
        current: int = 1,
        size: int = 10,
        status: str = "pending",
        app_slug: str | None = None,
    ) -> dict[str, Any]:
        """分页查询审核列表，联表 App + AppVersion。

        Args:
            current: 页码（从 1 开始）
            size: 每页数
            status: pending|approved|rejected|all
            app_slug: 按应用 slug 过滤（可选）

        Returns:
            {records: list[Row], total: int, current, size}
        """
        conditions = []
        if status != "all":
            conditions.append(AppReview.final_status == status)
        if app_slug:
            conditions.append(App.slug == app_slug)

        base_stmt = (
            select(AppReview, App, AppVersion)
            .join(App, App.id == AppReview.app_id)
            .join(AppVersion, AppVersion.id == AppReview.version_id)
        )
        if conditions:
            base_stmt = base_stmt.where(*conditions)

        # 总数
        count_stmt = (
            select(func.count())
            .select_from(AppReview)
            .join(App, App.id == AppReview.app_id)
            .join(AppVersion, AppVersion.id == AppReview.version_id)
        )
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = (await db.execute(count_stmt)).scalar_one()

        # 分页
        stmt = (
            base_stmt.order_by(AppReview.created_at.desc())
            .offset((current - 1) * size)
            .limit(size)
        )
        result = await db.execute(stmt)

        records = []
        for review, app, version in result:
            records.append(
                {
                    "id": review.id,
                    "app_id": app.id,
                    "app_name": app.name,
                    "app_slug": app.slug,
                    "version_id": version.id,
                    "version": version.version,
                    "final_status": review.final_status,
                    "ai_risk_level": review.ai_risk_level,
                    "reviewer_id": review.human_reviewer_id,
                    "created_at": review.created_at,
                    "human_reviewed_at": review.human_reviewed_at,
                }
            )

        return {
            "records": records,
            "total": total,
            "current": current,
            "size": size,
        }

    async def get_detail(self, db: AsyncSession, *, review_id: int) -> dict[str, Any]:
        """获取审核详情（联表 manifest）。

        Raises:
            NotFoundException: review_id 不存在
        """
        stmt = (
            select(AppReview, App, AppVersion)
            .join(App, App.id == AppReview.app_id)
            .join(AppVersion, AppVersion.id == AppReview.version_id)
            .where(AppReview.id == review_id)
        )
        row = (await db.execute(stmt)).first()
        if row is None:
            raise NotFoundException(
                resource_type="审核记录",
                error_code="APP_REVIEW_NOT_FOUND",
            )
        review, app, version = row
        return {
            "id": review.id,
            "app_id": app.id,
            "app_name": app.name,
            "app_slug": app.slug,
            "version_id": version.id,
            "version": version.version,
            "final_status": review.final_status,
            "ai_risk_level": review.ai_risk_level,
            "reviewer_id": review.human_reviewer_id,
            "created_at": review.created_at,
            "human_reviewed_at": review.human_reviewed_at,
            "manifest": version.manifest or {},
            "file_size": version.file_size,
            "rule_check_result": review.rule_check_result,
            "ai_report": review.ai_report,
            "human_comment": review.human_comment,
            "changelog": version.changelog,
        }

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
            rule_check_at=datetime.now(UTC),
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
        review.human_reviewed_at = datetime.now(UTC)
        review.final_status = "approved" if approved else "rejected"
        await db.flush()
        return review


review_service = ReviewService()
