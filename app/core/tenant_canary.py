"""Read-only, build-bound preflight facts for one hosted tenant canary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CanaryPhase = Literal["pre_activation", "post_activation"]
_BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True, order=True)
class CanaryRisk:
    code: str
    count: int = 1

    def as_dict(self) -> dict[str, str | int]:
        return {"code": self.code, "count": self.count}


@dataclass(frozen=True, slots=True)
class CanaryPreflightReport:
    build_sha: str
    phase: CanaryPhase
    target_tenant_id: int
    target_state: dict[str, str | int | None]
    non_target_active_count: int
    risks: tuple[CanaryRisk, ...]

    @property
    def risk_count(self) -> int:
        return sum(risk.count for risk in self.risks)

    def as_dict(self) -> dict:
        return {
            "schemaVersion": 1,
            "buildSha": self.build_sha,
            "phase": self.phase,
            "targetTenantId": str(self.target_tenant_id),
            "database": {
                "target": self.target_state,
                "nonTargetActiveCount": self.non_target_active_count,
            },
            "riskCount": self.risk_count,
            "risks": [risk.as_dict() for risk in self.risks],
            "secretsPersisted": False,
        }


async def build_canary_preflight_report(
    db: AsyncSession,
    *,
    build_sha: str,
    phase: CanaryPhase,
    target_tenant_id: int,
) -> CanaryPreflightReport:
    """Inspect only rollout-relevant registry facts in a caller-owned snapshot."""
    normalized_sha = build_sha.strip().lower()
    if _BUILD_SHA_RE.fullmatch(normalized_sha) is None:
        raise ValueError("build_sha must be a full Git SHA")
    if phase not in {"pre_activation", "post_activation"}:
        raise ValueError("invalid canary preflight phase")
    if (
        isinstance(target_tenant_id, bool)
        or not isinstance(target_tenant_id, int)
        or target_tenant_id <= 0
    ):
        raise ValueError("target_tenant_id must be a positive integer")

    target_result = await db.execute(
        text(
            "SELECT tenant_id, status, lifecycle_state, bootstrap_version, row_version "
            "FROM sys_tenant WHERE tenant_id = :target_tenant_id"
        ),
        {"target_tenant_id": target_tenant_id},
    )
    target = target_result.mappings().one_or_none()
    non_target_active_count = int(
        await db.scalar(
            text(
                "SELECT count(*) FROM sys_tenant "
                "WHERE tenant_id <> 0 AND tenant_id <> :target_tenant_id "
                "AND status = '1' AND lifecycle_state = 'active'"
            ),
            {"target_tenant_id": target_tenant_id},
        )
        or 0
    )

    risks: list[CanaryRisk] = []
    if target is None:
        target_state: dict[str, str | int | None] = {
            "status": None,
            "lifecycleState": None,
            "bootstrapVersion": None,
            "rowVersion": None,
        }
        risks.append(CanaryRisk("CANARY_TARGET_NOT_FOUND"))
    else:
        target_state = {
            "status": str(target["status"]),
            "lifecycleState": str(target["lifecycle_state"]),
            "bootstrapVersion": int(target["bootstrap_version"]),
            "rowVersion": int(target["row_version"]),
        }
        if int(target["bootstrap_version"]) < 1:
            risks.append(CanaryRisk("CANARY_TARGET_NOT_BOOTSTRAPPED"))
        if int(target["row_version"]) < 1:
            risks.append(CanaryRisk("CANARY_TARGET_VERSION_INVALID"))
        expected = ("2", "prepared") if phase == "pre_activation" else ("1", "active")
        actual = (str(target["status"]), str(target["lifecycle_state"]))
        if actual != expected:
            risks.append(
                CanaryRisk(
                    "CANARY_TARGET_NOT_PREPARED"
                    if phase == "pre_activation"
                    else "CANARY_TARGET_NOT_ACTIVE"
                )
            )
    if non_target_active_count:
        risks.append(CanaryRisk("NON_TARGET_TENANT_ACTIVE", non_target_active_count))

    return CanaryPreflightReport(
        build_sha=normalized_sha,
        phase=phase,
        target_tenant_id=target_tenant_id,
        target_state=target_state,
        non_target_active_count=non_target_active_count,
        risks=tuple(sorted(risks)),
    )
