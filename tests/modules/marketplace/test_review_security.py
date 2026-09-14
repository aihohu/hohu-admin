"""Security gates for Marketplace approval transitions."""

import pytest

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.models import App, AppReview, AppVersion
from app.modules.marketplace.service.review_service import review_service
from modules.marketplace import DEFAULT_TENANT


async def test_human_approval_revalidates_persisted_manifest(db_session) -> None:
    app = App(
        tenant_id=0,
        name="Legacy",
        slug="legacy-slug",
        type="lowcode",
        category="business",
        status="draft",
    )
    db_session.add(app)
    await db_session.flush()
    version = AppVersion(
        app_id=app.id,
        version="1.0.0",
        manifest={
            "name": "Legacy",
            "slug": "legacy_slug",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        },
        file_url="/uploads/legacy.zip",
        file_hash="0" * 64,
        review_status="pending",
    )
    db_session.add(version)
    await db_session.flush()
    review = AppReview(
        app_id=app.id,
        version_id=version.id,
        rule_check_result={"passed": True},
        human_status="pending",
        ai_risk_level="skipped",
        final_status="pending",
    )
    db_session.add(review)
    await db_session.flush()

    with pytest.raises(AppInvalidManifestException):
        await review_service.human_review(
            db_session,
            review_id=review.id,
            reviewer_id=1,
            approved=True,
            tenant=DEFAULT_TENANT,
        )

    assert review.human_status == "pending"
    assert review.final_status == "pending"
