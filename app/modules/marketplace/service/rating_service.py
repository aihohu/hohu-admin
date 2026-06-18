"""应用市场 - 评分 service（spec 14.6）

app.avg_rating / rating_count 是缓存字段，每次写入评分后同步更新（spec 14.6
反范式字段维护）。一人一评：UNIQUE(app_id, user_id)，重复评分抛 DuplicateException。
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DuplicateException, NotFoundException
from app.modules.marketplace.exceptions import AppErrorCode
from app.modules.marketplace.models import App, AppRating
from app.modules.marketplace.schemas.rating import RatingCreate


class RatingService:
    """评分 service（spec 14.6）

    app.avg_rating / rating_count 是缓存字段，每次写入评分后同步更新（spec 14.6
    反范式字段维护）。
    """

    async def create(
        self, db: AsyncSession, req: RatingCreate, *, user_id: int
    ) -> AppRating:
        """创建评分（一人一评，重复抛 DuplicateException）。

        Args:
            db: 数据库会话（调用方负责 commit）
            req: 评分请求（app_id 为 Snowflake 字符串）
            user_id: 评分用户ID

        Raises:
            DuplicateException: 该用户已对当前应用评过分
        """
        app_id = int(req.app_id)
        # 预检（friendly fast-path，并发兜底靠 DB UNIQUE）
        existing = await db.execute(
            select(AppRating).where(
                AppRating.app_id == app_id,
                AppRating.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateException(
                field="user_id",
                value=str(user_id),
                error_code=AppErrorCode.RATING_DUPLICATE,
            )

        rating = AppRating(
            app_id=app_id,
            user_id=user_id,
            rating=req.rating,
            comment=req.comment,
        )
        db.add(rating)
        try:
            await db.flush()
        except IntegrityError as e:
            # 并发兜底：DB UNIQUE(uq_mk_app_rating_app_user) 冲突翻译为 DuplicateException
            if "uq_mk_app_rating_app_user" in str(e.orig):
                raise DuplicateException(
                    field="user_id",
                    value=str(user_id),
                    error_code=AppErrorCode.RATING_DUPLICATE,
                ) from e
            raise
        await self._recompute_app_rating(db, app_id=app_id)
        return rating

    async def update(
        self,
        db: AsyncSession,
        *,
        app_id: int,
        user_id: int,
        rating: int,
        comment: str | None = None,
    ) -> AppRating:
        """更新评分（评分值必传，comment 可选）。

        Raises:
            NotFoundException: 该用户未对当前应用评分
        """
        record = await self._get_user_rating(db, app_id=app_id, user_id=user_id)
        record.rating = rating
        if comment is not None:
            record.comment = comment
        await db.flush()
        await self._recompute_app_rating(db, app_id=app_id)
        return record

    async def delete(self, db: AsyncSession, *, app_id: int, user_id: int) -> None:
        """删除评分。

        Raises:
            NotFoundException: 该用户未对当前应用评分
        """
        record = await self._get_user_rating(db, app_id=app_id, user_id=user_id)
        await db.delete(record)
        await db.flush()
        await self._recompute_app_rating(db, app_id=app_id)

    async def _get_user_rating(
        self, db: AsyncSession, *, app_id: int, user_id: int
    ) -> AppRating:
        result = await db.execute(
            select(AppRating).where(
                AppRating.app_id == app_id,
                AppRating.user_id == user_id,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundException(
                resource_type="评分",
                error_code=AppErrorCode.RATING_NOT_FOUND,
            )
        return record

    async def _recompute_app_rating(self, db: AsyncSession, *, app_id: int) -> None:
        """重算 app.avg_rating / rating_count（spec 14.6 重算 SQL）

        使用 func.coalesce(avg, 0) 防止无评分时 avg 为 NULL；
        rating_count 用 count(*) 直接得出。

        实现：直接加载 App 实例（命中 session identity map 时无 DB IO），
        然后 ORM 层 set rating_count/avg_rating 触发 UPDATE，
        保证内存对象与 DB 同步（Core update 会旁路 ORM 身份映射）。
        """
        stmt = select(
            func.count(AppRating.id),
            func.coalesce(func.avg(AppRating.rating), 0),
        ).where(AppRating.app_id == app_id)
        result = await db.execute(stmt)
        count, avg = result.one()
        app = await db.get(App, app_id)
        if app is not None:
            app.rating_count = count
            app.avg_rating = round(float(avg), 1)


rating_service = RatingService()
