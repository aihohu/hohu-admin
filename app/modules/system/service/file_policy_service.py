"""Single source for tenant upload limits and client-facing capabilities."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.system.service.settings_service import settings_service
from app.modules.system.settings_catalog import ALLOWED_UPLOAD_EXTENSIONS

MIB = 1024 * 1024
# Processing constraints are deliberately independent from upload preferences.
SCENARIO_CAPS = {
    "general": 100 * MIB,
    "image": 20 * MIB,
    "import": 10 * MIB,
    "ai_file": 10 * MIB,
}
SCENARIO_EXTENSIONS = {
    "general": ALLOWED_UPLOAD_EXTENSIONS,
    "image": frozenset({".jpg", ".jpeg", ".png"}),
    "import": frozenset({".csv", ".xlsx"}),
    "ai_file": frozenset({".csv", ".xlsx", ".txt", ".md", ".json"}),
}


@dataclass(frozen=True)
class UploadPolicy:
    max_bytes: int
    extensions: frozenset[str]

    def validate_extension(self, filename: str) -> None:
        extension = "." + filename.rsplit(".", 1)[-1].lower()
        if extension not in self.extensions:
            raise BusinessRuleException(
                "File type is not allowed", error_code="FILE_TYPE_NOT_ALLOWED"
            )

    def validate_size(self, size: int) -> None:
        if size < 0 or size > self.max_bytes:
            raise BusinessRuleException(
                "File exceeds the upload limit", error_code="FILE_TOO_LARGE"
            )


class FilePolicyService:
    async def resolve(
        self, db: AsyncSession, scenario: str, *, tenant: TenantContext
    ) -> UploadPolicy:
        if scenario not in SCENARIO_CAPS:
            raise ValueError("Unknown upload scenario")
        values = await settings_service.values(db, tenant_id=tenant.tenant_id)
        limit = int(values["upload:max_bytes"])
        if scenario in {"image", "import", "ai_file"}:
            field = "image" if scenario == "image" else "import"
            limit = min(limit, int(values[f"upload:{field}_max_bytes"]))
        extensions = frozenset(str(values["upload:allowed_extensions"]).split(","))
        return UploadPolicy(
            max(0, min(limit, settings.UPLOAD_HARD_MAX_BYTES, SCENARIO_CAPS[scenario])),
            extensions & SCENARIO_EXTENSIONS[scenario],
        )

    async def describe(self, db: AsyncSession, *, tenant: TenantContext) -> dict:
        result = {}
        for scenario in SCENARIO_CAPS:
            policy = await self.resolve(db, scenario, tenant=tenant)
            result[scenario] = {
                "maxBytes": policy.max_bytes,
                "extensions": sorted(policy.extensions),
            }
        return result


file_policy_service = FilePolicyService()
