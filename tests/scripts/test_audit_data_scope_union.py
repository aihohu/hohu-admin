"""Phase 2 read-only data-scope union audit tests."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
)
from app.core.id_generator import next_id
from app.db.base import role_depts, user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from scripts import audit_data_scope_union
from scripts.audit_data_scope_union import (
    ScopeUnionAuditReport,
    _parse_args,
    audit_exit_code,
    build_scope_union_report,
    release_gate_exit_code,
    verify_scope_union_ack,
    write_protected_report,
)

pytest_plugins = ("tests.modules.system.conftest",)


async def _row_counts(db: AsyncSession) -> tuple[int, int, int]:
    return (
        int(await db.scalar(select(func.count()).select_from(Role)) or 0),
        int(await db.scalar(select(func.count()).select_from(Dept)) or 0),
        int(await db.scalar(select(func.count()).select_from(User)) or 0),
    )


async def test_report_is_canonical_read_only_and_detects_union_expansion(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    own_dept = Dept(
        dept_id=next_id(),
        dept_name=f"audit-own-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom_dept = Dept(
        dept_id=next_id(),
        dept_name=f"audit-custom-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    dept_role = Role(
        role_id=next_id(),
        role_name=f"audit-dept-role-{marker}",
        role_code=f"R_AUDIT_DEPT_{marker}",
        data_scope=DATA_SCOPE_DEPT,
        status=STATUS_ENABLED,
    )
    custom_role = Role(
        role_id=next_id(),
        role_name=f"audit-custom-role-{marker}",
        role_code=f"R_AUDIT_CUSTOM_{marker}",
        data_scope=DATA_SCOPE_CUSTOM,
        status=STATUS_ENABLED,
    )
    actor = User(
        user_id=next_id(),
        user_name=f"audit-actor-{marker}",
        nickname="audit actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    actor.roles = [dept_role, custom_role]
    actor.depts = [own_dept]
    custom_user = User(
        user_id=next_id(),
        user_name=f"audit-custom-user-{marker}",
        nickname="audit custom user",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    db_session.add_all(
        [own_dept, custom_dept, dept_role, custom_role, actor, custom_user]
    )
    await db_session.flush()
    await db_session.execute(
        insert(role_depts).values(
            role_id=custom_role.role_id,
            dept_id=custom_dept.dept_id,
        )
    )
    await db_session.execute(
        insert(user_depts).values(
            user_id=custom_user.user_id,
            dept_id=custom_dept.dept_id,
            is_primary="N",
        )
    )
    await db_session.flush()
    before = await _row_counts(db_session)

    first = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )
    second = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )

    after = await _row_counts(db_session)
    principal = next(
        item
        for item in first.payload["principals"]
        if item["userId"] == str(actor.user_id)
    )
    assert before == after
    assert first.report_sha256 == second.report_sha256
    assert first.payload["schemaVersion"] == 2
    assert len(first.report_sha256) == 64
    assert first.expansion_count >= 1
    assert principal["roleCodes"] == sorted(
        [dept_role.role_code, custom_role.role_code]
    )
    assert str(custom_dept.dept_id) in principal["addedFromLegacyApiDeptIds"]
    assert str(custom_user.user_id) in principal["addedFromLegacyApiUserIds"]
    assert str(custom_dept.dept_id) in principal["addedFromLegacyAiDeptIds"]
    assert str(custom_user.user_id) in principal["addedFromLegacyAiUserIds"]
    assert first.payload["tenantId"] == "0"
    assert audit_exit_code(first) != 0
    assert verify_scope_union_ack(first, first.report_sha256) is True
    assert verify_scope_union_ack(first, "0" * 64) is False
    assert audit_exit_code(first, acknowledged_sha256=first.report_sha256) == 0


async def test_report_hash_changes_when_authorization_facts_drift(
    db_session: AsyncSession,
) -> None:
    first = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )
    marker = next_id()
    db_session.add(
        Dept(
            dept_id=next_id(),
            dept_name=f"audit-drift-{marker}",
            ancestors="0",
            order_num=0,
            status=STATUS_ENABLED,
        )
    )
    await db_session.flush()

    second = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )

    assert (
        first.payload["versions"]["departments"]
        != second.payload["versions"]["departments"]
    )
    assert first.report_sha256 != second.report_sha256

    db_session.add(
        Role(
            role_id=next_id(),
            role_name=f"audit-drift-role-{marker}",
            role_code=f"R_AUDIT_DRIFT_{marker}",
            status=STATUS_ENABLED,
        )
    )
    await db_session.flush()
    third = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )
    assert second.payload["versions"]["roles"] != third.payload["versions"]["roles"]
    assert second.report_sha256 != third.report_sha256

    db_session.add(
        User(
            user_id=next_id(),
            user_name=f"audit-drift-user-{marker}",
            nickname="audit drift user",
            hashed_password="x",
            status=STATUS_ENABLED,
        )
    )
    await db_session.flush()
    fourth = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )
    assert (
        third.payload["versions"]["memberships"]
        != fourth.payload["versions"]["memberships"]
    )
    assert third.report_sha256 != fourth.report_sha256


def test_protected_report_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "scope-diff.json"
    report = ScopeUnionAuditReport(
        payload={"schemaVersion": 1},
        report_sha256="a" * 64,
        expansion_count=1,
    )

    write_protected_report(output, report)

    assert json.loads(output.read_text(encoding="utf-8"))["reportSha256"] == ("a" * 64)
    with pytest.raises(FileExistsError):
        write_protected_report(output, report)


def test_release_gate_always_requires_the_exact_current_hash() -> None:
    report = ScopeUnionAuditReport(
        payload={"schemaVersion": 1},
        report_sha256="b" * 64,
        expansion_count=0,
    )

    assert release_gate_exit_code(report, None) != 0
    assert release_gate_exit_code(report, "B" * 64) != 0
    assert release_gate_exit_code(report, report.report_sha256) == 0


async def test_report_separates_legacy_api_and_ai_self_semantics(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    custom_dept = Dept(
        dept_id=next_id(),
        dept_name=f"audit-legacy-custom-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom_role = Role(
        role_id=next_id(),
        role_name=f"audit-legacy-custom-role-{marker}",
        role_code=f"R_AUDIT_LEGACY_CUSTOM_{marker}",
        data_scope=DATA_SCOPE_CUSTOM,
        status=STATUS_ENABLED,
    )
    self_role = Role(
        role_id=next_id(),
        role_name=f"audit-legacy-self-role-{marker}",
        role_code=f"R_AUDIT_LEGACY_SELF_{marker}",
        data_scope=DATA_SCOPE_SELF,
        status=STATUS_ENABLED,
    )
    actor = User(
        user_id=next_id(),
        user_name=f"audit-legacy-actor-{marker}",
        nickname="audit legacy actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    actor.roles = [custom_role, self_role]
    custom_user = User(
        user_id=next_id(),
        user_name=f"audit-legacy-user-{marker}",
        nickname="audit legacy user",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    db_session.add_all([custom_dept, custom_role, self_role, actor, custom_user])
    await db_session.flush()
    await db_session.execute(
        insert(role_depts).values(
            role_id=custom_role.role_id,
            dept_id=custom_dept.dept_id,
        )
    )
    await db_session.execute(
        insert(user_depts).values(
            user_id=custom_user.user_id,
            dept_id=custom_dept.dept_id,
            is_primary="N",
        )
    )
    await db_session.flush()

    report = await build_scope_union_report(
        db_session,
        build_sha="test-build-sha",
    )

    principal = next(
        item
        for item in report.payload["principals"]
        if item["userId"] == str(actor.user_id)
    )
    assert str(actor.user_id) in principal["addedFromLegacyApiUserIds"]
    assert str(actor.user_id) not in principal["addedFromLegacyAiUserIds"]


def test_release_args_reject_untrusted_tenant_and_incomplete_commands(
    tmp_path: Path,
) -> None:
    output = str(tmp_path / "scope-diff.json")

    with pytest.raises(SystemExit):
        _parse_args(["--output", output, "--tenant-id", "42"])
    with pytest.raises(SystemExit):
        _parse_args(["--output", output, "--verify-ack"])

    args = _parse_args(
        [
            "--output",
            output,
            "--verify-ack",
            "--maintenance-command",
            '["systemctl","stop","hohu-admin"]',
            "--switch-command",
            '["/opt/hohu/bin/activate-phase2"]',
        ]
    )
    assert args.maintenance_command == ("systemctl", "stop", "hohu-admin")
    assert args.switch_command == ("/opt/hohu/bin/activate-phase2",)


async def test_locked_release_holds_lock_through_switch(monkeypatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def hold_lock(_connection):
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    async def run_command(command: tuple[str, ...]) -> None:
        events.append(command[0])

    async def audit_callback() -> tuple[ScopeUnionAuditReport, int]:
        events.append("audit")
        return (
            ScopeUnionAuditReport(
                payload={"schemaVersion": 2},
                report_sha256="c" * 64,
                expansion_count=0,
            ),
            0,
        )

    monkeypatch.setattr(
        audit_data_scope_union,
        "_hold_authorization_migration",
        hold_lock,
    )
    monkeypatch.setattr(
        audit_data_scope_union,
        "_run_controlled_command",
        AsyncMock(side_effect=run_command),
    )

    await audit_data_scope_union._run_locked_release(
        MagicMock(),
        maintenance_command=("stop",),
        switch_command=("switch",),
        audit_callback=audit_callback,
    )

    assert events == ["lock", "stop", "audit", "switch", "unlock"]


async def test_locked_release_keeps_maintenance_when_ack_fails(monkeypatch) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def hold_lock(_connection):
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    async def run_command(command: tuple[str, ...]) -> None:
        events.append(command[0])

    async def audit_callback() -> tuple[ScopeUnionAuditReport, int]:
        events.append("audit")
        return (
            ScopeUnionAuditReport(
                payload={"schemaVersion": 2},
                report_sha256="d" * 64,
                expansion_count=1,
            ),
            2,
        )

    monkeypatch.setattr(
        audit_data_scope_union,
        "_hold_authorization_migration",
        hold_lock,
    )
    monkeypatch.setattr(
        audit_data_scope_union,
        "_run_controlled_command",
        AsyncMock(side_effect=run_command),
    )

    _report, exit_code = await audit_data_scope_union._run_locked_release(
        MagicMock(),
        maintenance_command=("stop",),
        switch_command=("switch",),
        audit_callback=audit_callback,
    )

    assert exit_code == 2
    assert events == ["lock", "stop", "audit", "unlock"]
