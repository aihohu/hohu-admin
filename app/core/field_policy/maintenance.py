"""Persistent preparation fence shared by all normal resume entry points."""

from app.core.exceptions import BusinessException


def require_normal_resume(guard):
    if (guard.directory / "policy-prepare.json").exists():
        raise BusinessException(
            409, "通用字段策略仍在准备中", error_code="FIELD_POLICY_ACTIVATION_BLOCKED"
        )
