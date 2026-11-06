"""Frozen inputs and bounded pure validation for explicit AI catalog candidates."""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType

from app.core.exceptions import BusinessException, BusinessRuleException
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import _order_error
from app.modules.ai.core.generation_settings import generation_settings
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.schemas.agent_admin import AgentAdminUpdateReq
from app.modules.ai.schemas.model import ModelCreate, ModelUpdate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderModelTestDraftRequest,
    ProviderUpdate,
)
from app.modules.ai.schemas.role_agent import RoleAgentBindReq
from app.modules.ai.service.role_agent import role_agent_service
from app.modules.platform.schemas import (
    PlatformTenantModelPoliciesPut,
    PlatformTenantModelPolicyPut,
)


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    global_write: bool = True
    production_connected: bool = False


CATALOG_CANDIDATE_ENTRIES = MappingProxyType(
    {
        "agent.update": CatalogEntry("AgentAdminService.update_agent"),
        "provider.create": CatalogEntry("ProviderService.create"),
        "provider.update": CatalogEntry("ProviderService.update"),
        "provider.delete": CatalogEntry("ProviderService.delete"),
        "model.create": CatalogEntry("ModelService.create"),
        "model.update": CatalogEntry("ModelService.update"),
        "model.delete": CatalogEntry("ModelService.delete"),
        "model_policy.put_many": CatalogEntry(
            "TenantModelPolicyAdminService.put_many", False
        ),
        "model_policy.put": CatalogEntry("TenantModelPolicyAdminService.put", False),
        "model_policy.delete": CatalogEntry(
            "TenantModelPolicyAdminService.delete", False
        ),
        "role.agents": CatalogEntry("RoleAgentService.put_binding", False),
    }
)
DNS_OPERATIONS = frozenset(
    {"provider.create", "provider.update", "model.create", "model.update"}
)
PROBE_OPERATIONS = frozenset({"provider.probe", "provider.probe_draft"})
_SCHEMAS = {
    "agent.update": AgentAdminUpdateReq,
    "provider.create": ProviderCreate,
    "provider.update": ProviderUpdate,
    "model.create": ModelCreate,
    "model.update": ModelUpdate,
    "model_policy.put_many": PlatformTenantModelPoliciesPut,
    "model_policy.put": PlatformTenantModelPolicyPut,
    "role.agents": RoleAgentBindReq,
    "provider.probe_draft": ProviderModelTestDraftRequest,
}


def limit_error():
    return BusinessException(
        422,
        "授权输入或影响集合超过上限",
        error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def stale():
    return BusinessException(
        409, "目录或授权准备快照已变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
    )


def wire(value):
    def encode(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise _order_error()

    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=encode
    ).encode("utf-8")


def bounded_json(value):
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 1024 or depth > 8:
            raise limit_error()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise _order_error()
            for nested in item.values():
                visit(nested, depth + 1)
        elif type(item) is list:
            for nested in item:
                visit(nested, depth + 1)
        elif type(item) not in {str, bool, int, float, type(None)}:
            raise _order_error()
        elif type(item) is float and not math.isfinite(item):
            raise _order_error()

    visit(value, 0)
    if len(wire(value)) > 64 * 1024:
        raise limit_error()


def _agent(data):
    if {"agent_id", "code", "is_builtin"} & data.keys():
        raise BusinessRuleException(
            "Agent identity 字段不可修改", error_code="AI_AGENT_IMMUTABLE_FIELD"
        )
    description = data.get("description")
    if description is not None and not 50 <= len(description) <= 200:
        raise BusinessRuleException(
            "description 长度必须在 50-200 字之间",
            error_code="AI_AGENT_DESC_LENGTH_INVALID",
        )
    pref = data.get("model_preference")
    if pref is not None and not re.fullmatch(
        r"(?:[1-9][0-9]*|[a-z0-9_-]+:[a-z0-9_-]+)", pref
    ):
        raise BusinessRuleException(
            "model_preference 必须为 modelId 或 'provider:model' 格式",
            error_code="AI_AGENT_MODEL_PREFERENCE_INVALID",
        )
    quota = data.get("daily_quota_per_user")
    if quota is not None and quota <= 0:
        raise BusinessRuleException(
            "daily_quota_per_user 必须 ≥ 1 或 null", error_code="AI_AGENT_QUOTA_INVALID"
        )
    if (
        len((data.get("system_prompt") or "").encode()) > 128 * 1024
        or len(wire(data)) > 160 * 1024
    ):
        raise limit_error()


def positive(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
        raise _order_error()
    return value


@dataclass(frozen=True, slots=True)
class CatalogRequest:
    tenant: TenantContext
    actor_user_version: int
    operation: str
    target_id: int | None
    target_tenant_id: int | None
    provider_id: int | None
    payload_bytes: bytes = field(repr=False)
    expected_bytes: bytes = field(repr=False)

    def payload(self):
        return json.loads(self.payload_bytes)


def freeze_request(
    *,
    tenant,
    actor_user_version,
    operation,
    payload=None,
    target_id=None,
    target_tenant_id=None,
    provider_id=None,
    expected_snapshot=None,
):
    if (
        type(tenant) is not TenantContext
        or operation not in set(CATALOG_CANDIDATE_ENTRIES) | PROBE_OPERATIONS
    ):
        raise _order_error()
    positive(actor_user_version)
    for value in (target_id, provider_id):
        if value is not None:
            positive(value)
    if target_tenant_id is not None:
        positive(target_tenant_id, zero=True)
    schema = _SCHEMAS.get(operation)
    if schema:
        if type(payload) is not schema:
            raise _order_error()
        # Enforce recursion budgets before Pydantic recursively serializes config.
        config = getattr(payload, "config", None)
        bounded_json(config)
        if operation == "provider.probe_draft":
            bounded_json(payload.model.config)
        raw = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
        clean = schema.model_validate(raw)
        data = clean.model_dump(
            mode="json", exclude_unset=operation.endswith(".update")
        )
    elif payload is None:
        data = {}
    else:
        raise _order_error()
    if operation == "agent.update":
        _agent(data)
    if operation == "role.agents":
        if len(data["agent_ids"]) > 256:
            raise limit_error()
        role_agent_service._normalize_agent_ids(data["agent_ids"])
    if "config" in data:
        provider_egress.validate_adapter_config(data["config"])
        if operation.startswith("model."):
            generation_settings(data["config"])
    capabilities = data.get("capabilities")
    if capabilities is not None and (
        len(capabilities) > 32 or any(len(value) > 64 for value in capabilities)
    ):
        raise limit_error()
    if operation.startswith("model_policy."):
        if target_tenant_id is None:
            raise _order_error()
    elif target_tenant_id is not None:
        raise _order_error()
    if operation.startswith("model.") or operation == "provider.probe":
        if provider_id is None:
            raise _order_error()
    elif provider_id is not None:
        raise _order_error()
    needs_target = operation not in {
        "provider.create",
        "model.create",
        "model_policy.put_many",
        "provider.probe_draft",
    }
    if (target_id is not None) != needs_target:
        raise _order_error()
    # Approval snapshots are a bounded authorization fact set, not adapter JSON.
    # A valid 1,100-member snapshot must not inherit the 1,024 config-node cap.
    if len(wire(expected_snapshot)) > 64 * 1024 * 1024:
        raise limit_error()
    return CatalogRequest(
        tenant,
        actor_user_version,
        operation,
        target_id,
        target_tenant_id,
        provider_id,
        wire(data),
        wire(expected_snapshot),
    )
