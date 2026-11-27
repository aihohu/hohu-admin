"""JWT requests exercise real strict sessions, projection and write owners."""

from sqlalchemy import insert, select, update

from app.db.base import user_depts
from app.modules.auth import api as auth_api
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_list_profile_email_auth_and_capabilities_use_projection(http_system):
    client, plain = http_system
    client._transport.app.include_router(auth_api.router, prefix="/auth")
    async with plain() as setup:
        await setup.execute(
            update(User)
            .where(User.user_id == 101)
            .values(user_phone="13812341234", user_email="ab@example.com")
        )
        await setup.commit()
    listed = await client.get("/user/list")
    assert listed.status_code == 200, listed.text
    record = listed.json()["data"]["records"][0]
    assert record["userPhone"] == "138****1234" and record["userId"] == "101"
    assert record["fieldAccess"]["userPhone"]["read"] == "masked"
    assert record["fieldAccess"]["userPhone"]["update"] is True
    assert record["fieldAccess"]["userPhone"]["queryOps"] == ["contains"]
    profile = await client.get("/user/profile")
    assert profile.status_code == 200, profile.text
    assert profile.json()["data"]["nickname"] is None
    assert profile.json()["data"]["userPhone"] == "13812341234"
    email = await client.get("/auth/email")
    assert email.status_code == 200, email.text
    assert email.json()["data"]["email"] == "ab@example.com"
    identity = await client.get("/auth/getUserInfo")
    assert identity.status_code == 200, identity.text
    assert "userEmail" not in identity.json()["data"]
    assert "*" in identity.json()["data"]["buttons"]
    caps = await client.get("/user/field-capabilities")
    assert caps.status_code == 200, caps.text
    assert caps.json()["data"]["fieldAccess"]["userPhone"]["update"] is True


async def test_patch_http_preserves_absence_and_put_required_contract(http_system):
    client, plain = http_system
    response = await client.patch(
        "/user/101", json={"nickname": "new name", "userPhone": None}
    )
    assert response.status_code == 200, response.text
    async with plain() as db:
        row = (
            await db.execute(
                select(User.user_name, User.nickname, User.user_phone).where(
                    User.user_id == 101
                )
            )
        ).one()
        assert tuple(row) == ("planner", "new name", None)
    invalid = await client.put("/user/101", json={"nickname": "other"})
    assert invalid.status_code == 422
    masked = await client.patch("/user/101", json={"userPhone": "138****1234"})
    assert masked.status_code == 400
    assert masked.json()["errorCode"] == "FIELD_MASKED_INPUT_INVALID"


async def test_membership_flags_flow_through_relation_projection(http_system):
    client, plain = http_system
    async with plain() as setup:
        await setup.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Team", ancestors="0", status="1"
            )
        )
        await setup.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=101, dept_id=401, is_primary="Y"
            )
        )
        await setup.commit()
    response = await client.get("/dept/401/users")
    assert response.status_code == 200, response.text
    record = response.json()["data"]["records"][0]
    assert record["isMember"] is True and record["isPrimary"] is True
    assert record["fieldAccess"]["departments"]["read"] == "plain"
    assert record["fieldAccess"]["departments"]["replace"] is True
