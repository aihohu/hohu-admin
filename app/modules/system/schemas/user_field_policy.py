"""Closed control-plane requests. Field IDs are catalog IDs, never SQL paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.modules.system.field_policy.documents import canonical_rules
from app.modules.system.field_policy.evaluator import FieldGrant


class PolicyInput(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class PolicyRule(PolicyInput):
    field_id: str = Field(min_length=1, max_length=64)
    operation: Literal[
        "read",
        "export",
        "create",
        "update",
        "replace",
        "exact",
        "contains",
        "eq",
        "group",
        "distinct",
    ]
    purpose: Literal[
        "management.read",
        "management.create",
        "management.update",
        "management.relations",
        "management.query",
        "export",
        "import",
        "own-account",
    ]
    effect: Literal["allow", "deny"] = "allow"
    read: Literal["plain", "masked"] = "plain"
    mask_id: str | None = Field(None, max_length=64)


class PolicyDraft(PolicyInput):
    expected_version: int = Field(ge=0, lt=2**63 - 1, strict=True)
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules: list[PolicyRule] = Field(max_length=256)

    def grants(self, role_id: int) -> tuple[FieldGrant, ...]:
        grants = tuple(
            FieldGrant(role_id=str(role_id), **rule.model_dump()) for rule in self.rules
        )
        canonical_rules(grants, role_id=role_id)
        return grants


class PolicyPublish(PolicyDraft):
    reason: str = Field(min_length=1, max_length=500)


class PolicyTransition(PolicyInput):
    expected_epoch: int = Field(ge=1, lt=2**63 - 1, strict=True)
    expected_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class PolicyStateOut(PolicyInput):
    mode: Literal["legacy", "ready", "enforcing", "frozen"]
    epoch: int
    catalog_digest: str
    candidate_manifest_digest: str | None
    active_manifest_digest: str | None
    ever_enforced: bool
    can_activate: bool
    activation_blockers: list[str]


class PolicyDocumentOut(PolicyInput):
    role_id: str
    revision_id: str
    version: int
    archived: bool
    rules_digest: str
    created_at: str
    rules: list[PolicyRule]


class PolicyResult(PolicyInput):
    policy: PolicyDocumentOut | None
    state: PolicyStateOut


class PolicyOption(PolicyInput):
    purpose: str
    operation: str


class PolicyFieldOut(PolicyInput):
    field_id: str
    readable: bool
    mask_id: str | None
    options: list[PolicyOption]


class PolicyReadResult(PolicyResult):
    catalog_revision: str
    catalog_digest: str
    fields: list[PolicyFieldOut]


class PolicyRuleValue(PolicyInput):
    effect: Literal["allow", "deny"]
    read: Literal["plain", "masked"]
    mask_id: str | None


class PolicyRuleChange(PolicyOption):
    field_id: str
    before: PolicyRuleValue | None
    after: PolicyRuleValue | None


class PolicyCapability(PolicyInput):
    allowed: bool
    read: Literal["hidden", "masked", "plain"]


class PolicyEffectiveSample(PolicyOption):
    subject_user_id: str
    record_user_id: str
    field_id: str
    before: PolicyCapability
    after: PolicyCapability


class PolicyPreviewResult(PolicyInput):
    expected_version: int
    preview_kind: Literal["rule-diff"]
    changes: list[PolicyRuleChange]
    state: PolicyStateOut
    effective_samples: list[PolicyEffectiveSample]
    samples_truncated: bool
    query_coverage_verified: Literal[False]
