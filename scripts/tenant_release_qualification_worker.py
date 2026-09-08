"""Internal database worker for the hosted release qualification runner.

This module is intentionally invoked only as a child process. Each invocation gets
one immutable Settings instance so the parent can exercise hosted and single modes
without mutating process-global configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import func, select

from app.core.id_generator import next_id
from app.core.redis import redis_client
from app.core.security import create_access_token, get_password_hash
from app.core.tenant import PlatformContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.platform.constants import (
    PLATFORM_TENANT_ACTIVATE,
    PLATFORM_TENANT_BOOTSTRAP,
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_WRITE,
)
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal
from app.modules.platform.tenant_bootstrap_service import tenant_bootstrap_service
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.menu import Menu
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.tenant_lifecycle_service import tenant_lifecycle_service

_CORRELATION_PREFIX = "plan7c-qualification-"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("stage", choices=("fresh", "seed", "snapshot", "token"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-tenant-id", type=int)
    parser.add_argument("--control-tenant-id", type=int)
    parser.add_argument("--tenant-id", type=int)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--tenant-version", type=int)
    return parser.parse_args()


def _write_json(path: Path | None, payload: dict) -> None:
    if path is None:
        raise ValueError("worker output path is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _platform(*, principal_id: int, permission: str, tenant_id: int) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=principal_id,
        actor_name="release-qualification",
        principal_type="service",
        permissions=frozenset({permission}),
        reason="Run isolated automated release qualification",
        ticket_id="PLAN7C-AUTOMATED-QUALIFICATION",
        correlation_id=f"{_CORRELATION_PREFIX}seed:{tenant_id}:{permission}",
        target_tenant_id=tenant_id,
    )


async def _fresh(output: Path | None) -> None:
    async with AsyncSessionLocal() as session:
        user_count = await session.scalar(select(func.count()).select_from(User)) or 0
        principal_count = (
            await session.scalar(select(func.count()).select_from(PlatformPrincipal))
            or 0
        )
        audit_count = (
            await session.scalar(select(func.count()).select_from(PlatformAuditLog))
            or 0
        )
        non_default_count = (
            await session.scalar(
                select(func.count()).select_from(Tenant).where(Tenant.tenant_id != 0)
            )
            or 0
        )
        redis_count = int(await redis_client.dbsize())
    payload = {
        "fresh": all(
            value == 0
            for value in (
                user_count,
                principal_count,
                audit_count,
                non_default_count,
                redis_count,
            )
        ),
        "userCount": user_count,
        "platformPrincipalCount": principal_count,
        "platformAuditCount": audit_count,
        "nonDefaultTenantCount": non_default_count,
        "redisDbSize": redis_count,
    }
    _write_json(output, payload)


async def _seed(output: Path | None) -> None:
    platform_password = os.environ.get(
        "HOHU_RELEASE_QUALIFICATION_PLATFORM_PASSWORD", ""
    )
    tenant_password = os.environ.get("HOHU_RELEASE_QUALIFICATION_TENANT_PASSWORD", "")
    if len(platform_password) < 12 or len(tenant_password) < 8:
        raise ValueError("qualification passwords are missing")

    async with AsyncSessionLocal() as session:
        principal = PlatformPrincipal(
            principal_name="release_qualification",
            display_name="Automated Release Qualification",
            hashed_password=get_password_hash(platform_password),
            permissions=sorted(
                {
                    PLATFORM_TENANT_READ,
                    PLATFORM_TENANT_WRITE,
                    PLATFORM_TENANT_BOOTSTRAP,
                    PLATFORM_TENANT_ACTIVATE,
                }
            ),
        )
        session.add(principal)
        provider = AiProvider(
            provider_code="release_qualification",
            name="Release Qualification Provider",
            api_key="ephemeral-encrypted-placeholder",
            base_url="http://127.0.0.1:9/v1",
            is_enabled=True,
            create_by="release-qualification",
        )
        session.add(provider)
        await session.flush()
        model = AiModel(
            provider_id=provider.provider_id,
            name="release-qualification-text",
            capabilities=["text"],
            is_enabled=True,
            create_by="release-qualification",
        )
        session.add(model)
        await session.flush()

        tenants: list[Tenant] = []
        for index, (code, name) in enumerate(
            (
                ("qualification-a", "Qualification Canary A"),
                ("qualification-b", "Qualification Control B"),
            ),
            start=1,
        ):
            tenant_id = next_id()
            tenant = await tenant_lifecycle_service.prepare_tenant(
                session,
                tenant_id=tenant_id,
                tenant_code=code,
                tenant_name=name,
                idempotency_key=f"plan7c-prepare-idempotency-000{index}",
                platform=_platform(
                    principal_id=principal.principal_id,
                    permission=PLATFORM_TENANT_WRITE,
                    tenant_id=tenant_id,
                ),
            )
            await tenant_bootstrap_service.bootstrap(
                session,
                tenant_id=tenant_id,
                default_model_id=model.model_id,
                admin_password=tenant_password,
                idempotency_key=f"plan7c-bootstrap-idempotency-000{index}",
                platform=_platform(
                    principal_id=principal.principal_id,
                    permission=PLATFORM_TENANT_BOOTSTRAP,
                    tenant_id=tenant_id,
                ),
            )
            tenants.append(tenant)

        await session.commit()
        users = (
            (
                await session.execute(
                    select(User)
                    .where(User.tenant_id.in_([tenant.tenant_id for tenant in tenants]))
                    .order_by(User.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        _write_json(
            output,
            {
                "principalName": principal.principal_name,
                "target": {
                    "tenantId": str(tenants[0].tenant_id),
                    "tenantCode": tenants[0].tenant_code,
                    "tenantVersion": tenants[0].row_version,
                    "userId": str(
                        next(
                            user.user_id
                            for user in users
                            if user.tenant_id == tenants[0].tenant_id
                        )
                    ),
                },
                "control": {
                    "tenantId": str(tenants[1].tenant_id),
                    "tenantCode": tenants[1].tenant_code,
                    "tenantVersion": tenants[1].row_version,
                    "userId": str(
                        next(
                            user.user_id
                            for user in users
                            if user.tenant_id == tenants[1].tenant_id
                        )
                    ),
                },
            },
        )


async def _tenant_snapshot(session, tenant_id: int) -> dict:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError("qualification tenant is missing")

    async def count(model) -> int:
        value = await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )
        return int(value or 0)

    return {
        "status": tenant.status,
        "lifecycleState": tenant.lifecycle_state,
        "rowVersion": tenant.row_version,
        "bootstrapVersion": tenant.bootstrap_version,
        "userCount": await count(User),
        "roleCount": await count(Role),
        "menuCount": await count(Menu),
        "modelPolicyCount": await count(TenantAiModelPolicy),
        "agentBindingCount": await count(RoleAiAgent),
        "loginLogCount": await count(SysLoginLog),
        "operationLogCount": await count(SysOperationLog),
    }


async def _snapshot(
    output: Path | None, target_tenant_id: int | None, control_tenant_id: int | None
) -> None:
    if target_tenant_id is None or control_tenant_id is None:
        raise ValueError("snapshot tenant ids are required")
    async with AsyncSessionLocal() as session:
        target = await _tenant_snapshot(session, target_tenant_id)
        control = await _tenant_snapshot(session, control_tenant_id)
        active_non_default = (
            await session.scalar(
                select(func.count())
                .select_from(Tenant)
                .where(Tenant.tenant_id != 0, Tenant.lifecycle_state == "active")
            )
            or 0
        )
        events = (
            (
                await session.execute(
                    select(PlatformAuditLog)
                    .where(
                        PlatformAuditLog.correlation_id.startswith(_CORRELATION_PREFIX)
                    )
                    .order_by(PlatformAuditLog.created_at, PlatformAuditLog.audit_id)
                )
            )
            .scalars()
            .all()
        )
    audit_events = [
        {
            "eventType": event.event_type,
            "statusCode": event.status_code,
            "correlationId": event.correlation_id,
            "targetKind": (
                "target"
                if event.target_tenant_id == target_tenant_id
                else "control"
                if event.target_tenant_id == control_tenant_id
                else "other"
            ),
        }
        for event in events
    ]
    _write_json(
        output,
        {
            "target": target,
            "control": control,
            "activeNonDefaultCount": int(active_non_default),
            "redisDbSize": int(await redis_client.dbsize()),
            "auditEvents": audit_events,
        },
    )


async def _token(
    tenant_id: int | None, user_id: int | None, tenant_version: int | None
) -> None:
    if tenant_id is None or user_id is None or tenant_version is None:
        raise ValueError("token identity is required")
    print(
        create_access_token(
            subject=str(user_id),
            tenant_id=tenant_id,
            tenant_version=tenant_version,
        )
    )


async def _main() -> None:
    arguments = _arguments()
    try:
        if arguments.stage == "fresh":
            await _fresh(arguments.output)
        elif arguments.stage == "seed":
            await _seed(arguments.output)
        elif arguments.stage == "snapshot":
            await _snapshot(
                arguments.output,
                arguments.target_tenant_id,
                arguments.control_tenant_id,
            )
        else:
            await _token(
                arguments.tenant_id,
                arguments.user_id,
                arguments.tenant_version,
            )
    finally:
        await redis_client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
