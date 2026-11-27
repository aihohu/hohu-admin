"""Principal security versions are global; login metadata is not authorization."""

import runpy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select

from app.core.authorization_maintenance import (
    StoppedWritersEvidence,
    acquire_maintenance_fence,
)
from app.core.exceptions import BusinessException
from app.core.security import create_platform_access_token, get_password_hash
from app.modules.platform import authorization_principal_candidate as principal_module
from app.modules.platform.authorization_principal_owner import principal_candidate_owner
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)

migration = runpy.run_path(
    str(
        Path(__file__).resolve().parents[3]
        / "alembic/versions/d01010100101_squash_to_head.py"
    )
)


@pytest.fixture
async def principal_sessions(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        for name in (
            "_CREATE_PRINCIPAL_VERSION_FUNCTION",
            "_CREATE_PRINCIPAL_VERSION_TRIGGER",
            "_CREATE_APPEND_ONLY_FUNCTION",
            "_CREATE_APPEND_ONLY_TRIGGER",
            "_CREATE_LINEAGE_FUNCTION",
            "_CREATE_LINEAGE_TRIGGER",
        ):
            await db.execute(migration[name])
        await db.execute(
            insert(PlatformPrincipal).values(
                principal_id=700,
                principal_name="control-admin",
                display_name="Control admin",
                hashed_password=get_password_hash("Principal123!"),
                permissions=["platform:ai:read"],
            )
        )
        await db.commit()
    return factory, plain


async def fence(action):
    async def observe():
        return StoppedWritersEvidence(
            "test",
            "0.2.0",
            "a" * 40,
            "b" * 64,
            datetime.now(UTC),
            frozenset({"api", "worker", "scheduler", "web-write-ingress"}),
            0,
            True,
        )

    return await acquire_maintenance_fence(
        observe, action=action, expected_build_sha="a" * 40
    )


async def test_login_metadata_does_not_change_global_or_tenant_epoch(
    principal_sessions,
):
    factory, plain = principal_sessions
    async with factory() as db:
        token = await principal_candidate_owner.login(
            db, principal_name="control-admin", password="Principal123!"
        )
        assert isinstance(token, str) and not db.in_transaction()
    async with factory() as db:
        result = await principal_candidate_owner.current(db, token=token)
        assert result.principal_name == "control-admin"
    async with plain() as db:
        row = await db.scalar(select(PlatformPrincipal))
        assert row.row_version == 1 and row.last_login_at is not None
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1


async def test_permission_replacement_bumps_all_once_and_replay_noop(
    principal_sessions,
):
    factory, plain = principal_sessions
    for expected in (True, False):
        permit = await fence("principal.permissions")
        async with factory() as db:
            result = await principal_candidate_owner.replace_permissions(
                db,
                principal_name="control-admin",
                current_password="Principal123!",
                permissions=["platform:ai:read", "platform:audit:retention"],
                fence=permit,
                audit_factory=factory,
                reason="test",
                ticket_id="P-1",
                correlation_id="permissions-test",
            )
            assert result is expected
    async with plain() as db:
        assert await db.scalar(select(PlatformPrincipal.row_version)) == 2
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
        assert (
            await db.execute(select(UserFieldPolicyState.epoch))
        ).scalars().all() == [2, 2]
        assert len((await db.execute(select(PlatformAuditLog.audit_id))).all()) == 4


async def test_first_principal_is_atomic_with_lineage_and_refuses_second(
    principal_sessions,
):
    factory, plain = principal_sessions
    async with plain() as db:
        await db.execute(delete(PlatformPrincipal))
        await db.commit()
    for attempt in range(2):
        permit = await fence("principal.create")
        async with factory() as db:
            request = {
                "principal_name": "first-admin",
                "display_name": "First",
                "password": "FirstPrincipal123!",
                "permissions": ["platform:ai:read"],
                "fence": permit,
                "reason": "first install",
                "ticket_id": "P-FIRST",
                "correlation_id": "first-create",
            }
            if attempt:
                with pytest.raises(BusinessException) as error:
                    await principal_candidate_owner.create_first(db, **request)
                assert error.value.error_code == "PLATFORM_PRINCIPAL_ALREADY_EXISTS"
            else:
                result = await principal_candidate_owner.create_first(db, **request)
                assert result["principal_name"] == "first-admin"
    async with plain() as db:
        assert len((await db.execute(select(PlatformPrincipal))).all()) == 1
        assert len((await db.execute(select(PlatformAuditLog))).all()) == 2
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2


async def test_wrong_password_cannot_login_or_create_audit(principal_sessions):
    factory, plain = principal_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await principal_candidate_owner.login(
                db, principal_name="control-admin", password="wrong"
            )
        assert not db.in_transaction()
    async with plain() as db:
        assert await db.scalar(select(PlatformPrincipal.last_login_at)) is None
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1


async def test_old_principal_token_is_rejected_after_permission_change(
    principal_sessions,
):
    factory, _ = principal_sessions
    async with factory() as db:
        token = await principal_candidate_owner.login(
            db, principal_name="control-admin", password="Principal123!"
        )
    permit = await fence("principal.permissions")
    async with factory() as db:
        await principal_candidate_owner.replace_permissions(
            db,
            principal_name="control-admin",
            current_password="Principal123!",
            permissions=["platform:audit:retention"],
            fence=permit,
            audit_factory=factory,
            reason="revoke",
            ticket_id="P-REVOKE",
            correlation_id="revoke-test",
        )
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await principal_candidate_owner.current(db, token=token)
        assert error.value.error_code == "PLATFORM_TOKEN_INVALID"


async def test_current_principal_enforces_cumulative_decoded_read_budget(
    principal_sessions, monkeypatch
):
    factory, _ = principal_sessions
    monkeypatch.setattr(
        principal_module,
        "bounded_authorization_reads",
        lambda db: bounded_authorization_reads(
            db, AuthorizationReadBudget(max_bytes=1)
        ),
    )
    token = create_platform_access_token(subject="700", principal_version=1)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await principal_candidate_owner.current(db, token=token)
        assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        assert not db.in_transaction()
