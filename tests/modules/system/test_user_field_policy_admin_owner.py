"""Real G/B/CAS owners; authorization is checked from current enabled roles."""

import pytest
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_policy_owner import UserFieldPolicyOwner
from app.db.base import Base, user_depts, user_roles
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyAudit as Audit
from app.modules.system.models.user_field_policy import UserFieldPolicyState as State
from app.modules.system.schemas.user_field_policy import (
    PolicyDraft,
    PolicyPublish,
    PolicyTransition,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from tests.modules.system.test_planned_user_field_policy import TENANT
from tests.modules.system.test_planned_user_field_policy import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_planned_user_field_policy import (
    policy_sessions as policy_sessions,
)


@pytest.fixture
async def admin_sessions(policy_sessions):
    factory, plain = policy_sessions
    async with plain.kw["bind"].begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[user_roles, user_depts])
        )
    async with plain() as db:
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=201)
        )
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_SUPER")
        )
        await db.commit()
    return factory, plain


async def grant(plain, permission):
    async with plain() as db:
        await db.execute(
            update(Menu).where(Menu.menu_id == 301).values(permission=permission)
        )
        await db.commit()


async def call(factory, operation, payload=None, role_id=201):
    async with factory() as db:
        return await UserFieldPolicyOwner().execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            payload=payload,
            role_id=role_id,
        )


async def test_super_admin_still_requires_explicit_policy_permission(admin_sessions):
    factory, plain = admin_sessions
    with pytest.raises(BusinessException) as exc:
        await call(factory, "read")
    assert exc.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"


async def test_publish_cas_and_preview_without_business_values(admin_sessions):
    factory, plain = admin_sessions
    await grant(plain, "system:user:field-policy-edit")
    draft = PolicyPublish(
        expectedVersion=0, catalogDigest=catalog_digest(), rules=[], reason="reviewed"
    )
    saved = await call(factory, "publish", draft)
    assert saved["policy"]["version"] == 1
    with pytest.raises(BusinessException) as exc:
        await call(factory, "publish", draft)
    assert exc.value.error_code == "FIELD_POLICY_VERSION_CONFLICT"
    async with plain() as db:
        assert len((await db.scalars(select(Audit))).all()) == 1
    await grant(plain, "system:user:field-policy-view")
    response = await call(factory, "read")
    assert response["state"]["canActivate"] is False
    assert response["policy"]["rules"] == []
    with pytest.raises(BusinessException):
        await call(factory, "read", role_id=202)


async def test_activation_cannot_be_enabled_by_a_client_request(admin_sessions):
    factory, plain = admin_sessions
    await grant(plain, "system:user:field-policy-activate")
    request = PolicyTransition(
        expectedEpoch=1, expectedManifestDigest="a" * 64, reason="try"
    )
    with pytest.raises(BusinessException) as exc:
        await call(factory, "activate", request)
    assert exc.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"
    async with plain() as db:
        assert not (await db.scalars(select(Audit))).all()


async def test_preview_reports_effective_samples_without_values(admin_sessions):
    factory, plain = admin_sessions
    await grant(plain, "system:user:field-policy-view")
    result = await call(
        factory,
        "preview",
        PolicyDraft(expectedVersion=0, catalogDigest=catalog_digest(), rules=[]),
    )
    assert result["effectiveSamples"]
    assert result["effectiveSamples"][0]["subjectUserId"] == "101"
    assert result["queryCoverageVerified"] is False
    assert "planner" not in str(result) and "not-a-credential" not in str(result)


async def test_public_activation_freeze_and_audit_failure_are_atomic(
    admin_sessions, monkeypatch
):
    factory, plain = admin_sessions
    # Gate parsing has separate fixture-based tests; this test exercises the
    # qualified control-plane transaction and its ACK/audit failure boundary.
    monkeypatch.setattr(
        "app.db.authorization_policy_owner.qualification_blockers", lambda: ()
    )
    await grant(plain, "system:user:field-policy-edit")
    saved = await call(
        factory,
        "publish",
        PolicyPublish(
            expectedVersion=0,
            catalogDigest=catalog_digest(),
            rules=[],
            reason="test candidate",
        ),
    )

    def transition(result):
        return PolicyTransition(
            expectedEpoch=result["state"]["epoch"],
            expectedManifestDigest=result["state"]["candidateManifestDigest"],
            reason="isolated activation",
        )

    ready = await call(factory, "ready", transition(saved))
    assert ready["state"]["canActivate"]
    await grant(plain, "system:user:field-policy-activate")
    original = policies._audit

    async def failed(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(policies, "_audit", failed)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await call(factory, "activate", transition(ready))
    async with plain() as db:
        assert (
            await db.scalar(select(State).where(State.tenant_id == 0))
        ).mode == "ready"
        assert len((await db.scalars(select(Audit))).all()) == 2
    monkeypatch.setattr(policies, "_audit", original)
    active = await call(factory, "activate", transition(ready))
    assert active["state"]["mode"] == "enforcing"
    frozen = await call(factory, "freeze", transition(active))
    assert frozen["state"]["mode"] == "frozen" and frozen["state"]["everEnforced"]
