# ruff: noqa: T201
"""审计存量 Provider/Model 出站配置；只报告 quarantine，不改写 enabled。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider


@dataclass(frozen=True)
class ProviderEgressFinding:
    object_type: str
    object_id: int
    provider_id: int
    status: str = "EGRESS_POLICY_BLOCKED"


@dataclass(frozen=True)
class ProviderEgressAuditReport:
    providers_checked: int
    models_checked: int
    findings: tuple[ProviderEgressFinding, ...]


async def audit_ai_provider_egress(
    db: AsyncSession,
) -> ProviderEgressAuditReport:
    providers = list((await db.scalars(select(AiProvider))).all())
    models = list((await db.scalars(select(AiModel))).all())
    providers_by_id = {provider.provider_id: provider for provider in providers}
    findings: list[ProviderEgressFinding] = []

    for provider in providers:
        if not await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            configs=(provider.config,),
        ):
            findings.append(
                ProviderEgressFinding(
                    object_type="provider",
                    object_id=provider.provider_id,
                    provider_id=provider.provider_id,
                )
            )

    for model in models:
        provider = providers_by_id.get(model.provider_id)
        if provider is None:
            findings.append(
                ProviderEgressFinding(
                    object_type="model",
                    object_id=model.model_id,
                    provider_id=model.provider_id,
                )
            )
            continue
        if not await provider_egress.is_configuration_allowed(
            provider.provider_code,
            provider.base_url,
            model_base_url=model.base_url,
            configs=(provider.config, model.config),
        ):
            findings.append(
                ProviderEgressFinding(
                    object_type="model",
                    object_id=model.model_id,
                    provider_id=provider.provider_id,
                )
            )

    return ProviderEgressAuditReport(
        providers_checked=len(providers),
        models_checked=len(models),
        findings=tuple(findings),
    )


async def main() -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            report = await audit_ai_provider_egress(db)
    finally:
        await engine.dispose()

    print(
        "AI Provider egress audit: "
        f"providers={report.providers_checked}, "
        f"models={report.models_checked}, blocked={len(report.findings)}"
    )
    for finding in report.findings:
        print(
            f"{finding.status} object_type={finding.object_type} "
            f"object_id={finding.object_id} provider_id={finding.provider_id}"
        )
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
