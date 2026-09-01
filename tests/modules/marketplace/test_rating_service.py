import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import DuplicateException, NotFoundException
from app.core.id_generator import next_id
from app.modules.marketplace.models import App
from app.modules.marketplace.schemas.rating import RatingCreate
from app.modules.marketplace.service.rating_service import rating_service
from app.modules.system.models import User


async def _create_user(db_session, name: str) -> User:
    """创建 sys_user 行，满足 mk_app_rating.user_id 的外键约束。"""
    user = User(
        tenant_id=0,
        user_id=next_id(),
        user_name=name,
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
async def sample_app(db_session):
    app = App(
        tenant_id=0,
        name="X",
        slug="rating-test-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(app)
    await db_session.flush()
    return app


class TestRatingService:
    async def test_create_updates_avg_rating(self, db_session, sample_app):
        """新装：评分 5+3 → avg 4.0，count 2"""
        user1 = await _create_user(db_session, "u1")
        user2 = await _create_user(db_session, "u2")
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=5),
            user_id=user1.user_id,
        )
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=3),
            user_id=user2.user_id,
        )
        await db_session.flush()

        app = await db_session.get(App, sample_app.id)
        assert app.rating_count == 2
        assert float(app.avg_rating) == 4.0  # (5+3)/2

    async def test_update_rating_recomputes_avg(self, db_session, sample_app):
        user1 = await _create_user(db_session, "u1")
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=5),
            user_id=user1.user_id,
        )
        await db_session.flush()
        await rating_service.update(
            db_session, app_id=sample_app.id, user_id=user1.user_id, rating=1
        )
        await db_session.flush()

        app = await db_session.get(App, sample_app.id)
        assert app.rating_count == 1
        assert float(app.avg_rating) == 1.0

    async def test_delete_rating_decrements_count(self, db_session, sample_app):
        user1 = await _create_user(db_session, "u1")
        user2 = await _create_user(db_session, "u2")
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=5),
            user_id=user1.user_id,
        )
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=3),
            user_id=user2.user_id,
        )
        await db_session.flush()
        await rating_service.delete(
            db_session, app_id=sample_app.id, user_id=user1.user_id
        )
        await db_session.flush()

        app = await db_session.get(App, sample_app.id)
        assert app.rating_count == 1
        assert float(app.avg_rating) == 3.0

    async def test_user_can_only_rate_once(self, db_session, sample_app):
        user1 = await _create_user(db_session, "u1")
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(sample_app.id), rating=5),
            user_id=user1.user_id,
        )
        await db_session.flush()
        with pytest.raises(DuplicateException):
            await rating_service.create(
                db_session,
                RatingCreate(app_id=str(sample_app.id), rating=4),
                user_id=user1.user_id,
            )

    async def test_update_nonexistent_raises(self, db_session, sample_app):
        # user_id=999 没在 sys_user 里，但是 update 走 _get_user_rating，
        # 查 mk_app_rating 表不会触发 FK 检查 → 抛 NotFoundException
        with pytest.raises(NotFoundException):
            await rating_service.update(
                db_session, app_id=sample_app.id, user_id=999, rating=5
            )

    async def test_concurrent_duplicate_rating_raises_duplicate(
        self, db_session, sample_app, monkeypatch
    ):
        """模拟 DB UNIQUE 冲突：预检返回 None，flush 抛 IntegrityError，
        验证抛 DuplicateException（而非 500 IntegrityError）"""
        user = await _create_user(db_session, "race-user")

        # 让预检 scalar_one_or_none 返回 None（模拟并发场景下两请求都通过预检）
        original_execute = db_session.execute
        call_count = [0]

        async def patched_execute(stmt):
            call_count[0] += 1
            # 第一次 execute 是预检 select(AppRating)
            if call_count[0] == 1:

                class MockResult:
                    def scalar_one_or_none(self):
                        return None

                return MockResult()
            return await original_execute(stmt)

        monkeypatch.setattr(db_session, "execute", patched_execute)

        # mock flush 抛 IntegrityError（模拟 DB UNIQUE 冲突）
        async def patched_flush():
            raise IntegrityError(
                "simulated",
                {},
                Exception(
                    "duplicate key value violates unique constraint "
                    '"uq_mk_app_rating_app_user"'
                ),
            )

        monkeypatch.setattr(db_session, "flush", patched_flush)

        with pytest.raises(DuplicateException):
            await rating_service.create(
                db_session,
                RatingCreate(app_id=str(sample_app.id), rating=5),
                user_id=user.user_id,
            )
