"""Dormant AI catalog writers using complete plans and server-issued audit intent."""

import asyncio
import hashlib
import json
import weakref
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.core.security import decrypt_value, encrypt_value
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.ai.core.generation_settings import generation_settings
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.agent_admin import AgentAdminDetailItem
from app.modules.ai.schemas.model import ModelOut
from app.modules.ai.schemas.provider import ProviderOut
from app.modules.ai.service.authorization_catalog_inputs import (
    CATALOG_CANDIDATE_ENTRIES,
    DNS_OPERATIONS,
    PROBE_OPERATIONS,
    CatalogRequest,
    bounded_json,
    freeze_request,
    stale,
    wire,
)
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.platform.constants import PLATFORM_AI_WRITE
from app.modules.platform.schemas import (
    PlatformTenantModelCatalogOut,
    PlatformTenantModelPolicyOut,
)
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
    read_policy_target,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.role_delegation_service import role_delegation_service

_TABLES = {
    ResourceKind.PROVIDER: AiProvider.__table__,
    ResourceKind.MODEL: AiModel.__table__,
    ResourceKind.AGENT: AiAgent.__table__,
    ResourceKind.TENANT_MODEL_POLICY: TenantAiModelPolicy.__table__,
    ResourceKind.ROLE_AGENT: RoleAiAgent.__table__,
}
_PK = {
    ResourceKind.PROVIDER: "provider_id",
    ResourceKind.MODEL: "model_id",
    ResourceKind.AGENT: "agent_id",
}
_PREPARATIONS = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class CatalogPreparation:
    request: CatalogRequest = field(repr=False)
    snapshot: bytes = field(repr=False)
    effective: bytes = field(repr=False)
    _session: object = field(repr=False)


@dataclass(slots=True, repr=False)
class _RoleAgentWork:
    transaction: object
    request: CatalogRequest
    identity: object
    facts: dict
    evaluation: object
    locks: LockPlan
    used: bool = False


def _fresh(db):
    if (
        not isinstance(db, AuthorizationPlanSession)
        or db.in_transaction()
        or db.new
        or db.dirty
        or db.deleted
    ):
        raise _order_error()


def _external(preparation, state):
    if (
        type(preparation) is not CatalogPreparation
        or _PREPARATIONS.get(preparation) != state
    ):
        raise _order_error()
    db = preparation._session()
    if db is not None and db.in_transaction():
        raise _order_error()


def _row(facts, kind, identifier):
    found = next((r for r in facts[kind] if r[_PK[kind]] == identifier), None)
    if found is None:
        names = {
            ResourceKind.PROVIDER: ("AI提供商", "AI_PROVIDER_NOT_FOUND"),
            ResourceKind.MODEL: ("AI模型", "AI_MODEL_NOT_FOUND"),
            ResourceKind.AGENT: ("AI Agent", "AI_AGENT_NOT_FOUND"),
        }
        resource, code = names[kind]
        raise NotFoundException(resource, error_code=code)
    return found


def _facts_bytes(facts):
    return wire({kind.value: rows for kind, rows in facts.items()})


def _key(kind, row, tenant_id=None):
    if kind is ResourceKind.ROLE_AGENT:
        return ResourceKey(kind, tenant_id, (row["role_id"], row["agent_id"]))
    return ResourceKey(kind, tenant_id, row[_PK.get(kind, "model_id")])


def _audit(audit, request):
    if request.operation == "role.agents" and audit is None:
        return
    from app.modules.system.service.authorization_control_identity import (  # noqa: PLC0415
        ControlAudit,
    )

    if type(audit) is not ControlAudit:
        raise _order_error()
    audit.assert_request(
        tenant=request.tenant,
        actor_user_version=request.actor_user_version,
        permission=PLATFORM_AI_WRITE,
        target_tenant_id=request.target_tenant_id,
        operation=request.operation,
    )


class CatalogAuthorizationCandidate:
    async def plan_role_agents(self, db, tx, request):
        tx.assert_session(db)
        if type(request) is not CatalogRequest or request.operation != "role.agents":
            raise _order_error()
        tid = request.tenant.tenant_id
        tx.assert_planning(tid, exclusive=True)
        identity = await self._identity(db, request)
        facts = await self._facts(db, request)
        evaluation = await self._delegation(db, request)
        expected = json.loads(request.expected_bytes)
        if expected is not None and evaluation.snapshot != expected:
            raise BusinessRuleException(
                "角色 Agent 审批快照已变化",
                error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
            )
        keys = set(identity.keys)
        for kind, rows in facts.items():
            keys.update(
                _key(kind, row, tid if kind is ResourceKind.ROLE_AGENT else None)
                for row in rows
            )
        for kind, ids in (
            (ResourceKind.ROLE, evaluation.role_ids),
            (ResourceKind.DEPT, evaluation.dept_ids),
            (ResourceKind.USER, evaluation.user_ids),
        ):
            keys.update(ResourceKey(kind, tid, value) for value in ids)
        locks = LockPlan(existing=tuple(keys), new=self._new_keys(request, facts, None))
        return _RoleAgentWork(tx, request, identity, facts, evaluation, locks)

    async def apply_role_agents(self, db, tx, work):
        tx.assert_session(db)
        if type(work) is not _RoleAgentWork or work.transaction is not tx or work.used:
            raise _order_error()
        work.used = True
        tx.assert_planned(work.locks)
        request = work.request
        await assert_identity(db, work.identity)
        if (
            _facts_bytes(await self._facts(db, request)) != _facts_bytes(work.facts)
            or await self._delegation(db, request) != work.evaluation
        ):
            raise stale()
        async with tx.aggregate(request.tenant.tenant_id):
            return await self._apply(
                db, tx, request, work.facts, None, work.identity.user_name
            )

    async def _facts(self, db, request):
        result = {}
        for kind in (ResourceKind.PROVIDER, ResourceKind.MODEL, ResourceKind.AGENT):
            table = _TABLES[kind]
            result[kind] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table).order_by(table.c[_PK[kind]]).limit(100001)
                    )
                ).mappings()
            ]
        if request.operation.startswith("model_policy."):
            table = TenantAiModelPolicy.__table__
            result[ResourceKind.TENANT_MODEL_POLICY] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table)
                        .where(table.c.tenant_id == request.target_tenant_id)
                        .order_by(table.c.model_id)
                        .limit(10001)
                    )
                ).mappings()
            ]
        if request.operation == "role.agents":
            table = RoleAiAgent.__table__
            result[ResourceKind.ROLE_AGENT] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table)
                        .where(
                            table.c.tenant_id == request.tenant.tenant_id,
                            table.c.role_id == request.target_id,
                        )
                        .order_by(table.c.agent_id)
                        .limit(100001)
                    )
                ).mappings()
            ]
        return result

    async def _identity(self, db, request):
        local = request.operation == "role.agents"
        return await read_identity(
            db,
            tenant=request.tenant,
            actor_user_version=request.actor_user_version,
            permissions=("system:role:ai-agent-auth",) if local else (),
            system=not local,
        )

    def _effective(self, request, facts):
        data = request.payload()
        op = request.operation
        if op.startswith("provider.") and op not in PROBE_OPERATIONS:
            old = (
                {}
                if op == "provider.create"
                else _row(facts, ResourceKind.PROVIDER, request.target_id)
            )
            merged = {**old, **data}
            return {
                "provider_code": merged["provider_code"],
                "base_url": merged["base_url"],
                "config": merged["config"],
            }
        if op == "provider.probe_draft":
            provider = (
                _row(facts, ResourceKind.PROVIDER, int(data["provider_id"]))
                if data.get("provider_id")
                else None
            )
            secret = data.get("api_key") or (
                decrypt_value(provider["api_key"]) if provider else None
            )
            if not secret:
                raise BusinessRuleException(
                    "请先填写 API Key 再测试模型",
                    error_code="AI_PROVIDER_TEST_KEY_REQUIRED",
                )
            return {
                "provider_code": data["provider_code"],
                "base_url": data.get("base_url"),
                "config": data.get("config"),
                "model": data["model"],
                "api_key": secret,
            }
        provider = _row(facts, ResourceKind.PROVIDER, request.provider_id)
        model = (
            {}
            if op == "model.create"
            else _row(facts, ResourceKind.MODEL, request.target_id)
        )
        if model and model["provider_id"] != request.provider_id:
            raise BusinessRuleException(
                "模型不属于指定 Provider", error_code="AI_PROVIDER_MODEL_MISMATCH"
            )
        merged = {**model, **data}
        effective = {
            "provider_code": provider["provider_code"],
            "base_url": provider["base_url"],
            "config": provider["config"],
            "model": merged,
        }
        if op == "provider.probe":
            effective["api_key"] = decrypt_value(provider["api_key"])
        return effective

    async def prepare(self, db, **kwargs):
        request = freeze_request(**kwargs)
        if request.operation not in DNS_OPERATIONS | PROBE_OPERATIONS:
            raise _order_error()
        _fresh(db)
        with bounded_authorization_reads(db):
            async with asyncio.timeout(5):
                await self._identity(db, request)
                facts = await self._facts(db, request)
                effective = self._effective(request, facts)
                bounded_json(effective["config"])
                provider_egress.validate_adapter_config(effective["config"])
                if "model" in effective:
                    model_config = effective["model"].get("config")
                    bounded_json(model_config)
                    provider_egress.validate_adapter_config(model_config)
                    generation_settings(model_config)
        prep = CatalogPreparation(
            request, _facts_bytes(facts), wire(effective), weakref.ref(db)
        )
        _PREPARATIONS[prep] = "read"
        return prep

    async def validate_preparation(self, preparation):
        _external(preparation, "read")
        if preparation.request.operation not in DNS_OPERATIONS:
            raise _order_error()
        _PREPARATIONS[preparation] = "validating"
        effective = json.loads(preparation.effective)
        async with asyncio.timeout(2):
            await provider_egress.validate_destination(
                effective["provider_code"], effective["base_url"]
            )
            if effective.get("model", {}).get("base_url"):
                await provider_egress.validate_destination(
                    effective["provider_code"], effective["model"]["base_url"]
                )
        _PREPARATIONS[preparation] = "validated"

    async def _delegation(self, db, request):
        return await role_delegation_service._evaluate_agent_replacement(
            db,
            actor_user_id=request.tenant.actor_user_id,
            tenant=request.tenant,
            role_id=request.target_id,
            new_agent_ids=tuple(sorted(int(v) for v in request.payload()["agent_ids"])),
        )

    def _new_keys(self, request, facts, new_id):
        op, data = request.operation, request.payload()
        if op in {"provider.create", "model.create"}:
            return (
                NewResource(
                    ResourceKey(
                        ResourceKind.PROVIDER
                        if op == "provider.create"
                        else ResourceKind.MODEL,
                        None,
                        new_id,
                    )
                ),
            )
        if op == "role.agents":
            stored = {row["agent_id"] for row in facts[ResourceKind.ROLE_AGENT]}
            return tuple(
                NewResource(
                    ResourceKey(
                        ResourceKind.ROLE_AGENT,
                        request.tenant.tenant_id,
                        (request.target_id, int(value)),
                    )
                )
                for value in data["agent_ids"]
                if int(value) not in stored
            )
        if op in {"model_policy.put", "model_policy.put_many"}:
            stored = {
                row["model_id"] for row in facts[ResourceKind.TENANT_MODEL_POLICY]
            }
            candidates = (
                data["policies"]
                if op.endswith("put_many")
                else [{**data, "model_id": request.target_id}]
            )
            return tuple(
                NewResource(
                    ResourceKey(
                        ResourceKind.TENANT_MODEL_POLICY,
                        request.target_tenant_id,
                        int(item["model_id"]),
                    )
                )
                for item in candidates
                if int(item["model_id"]) not in stored
                and (op.endswith(".put") or item["enabled"])
            )
        return ()

    async def execute(
        self, db, *, audit=None, preparation=None, http_result=False, **kwargs
    ):
        if type(http_result) is not bool:
            raise _order_error()
        request = freeze_request(**kwargs)
        _audit(audit, request)
        _fresh(db)
        if request.operation == "role.agents" and audit is None and preparation is None:
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope(
                    db,
                    tenants=(TenantLock(request.tenant.tenant_id, LockMode.EXCLUSIVE),),
                ) as tx:
                    work = await self.plan_role_agents(db, tx, request)
                    await tx.lock(work.locks)
                    return await self.apply_role_agents(db, tx, work)
        if request.operation in DNS_OPERATIONS:
            _external(preparation, "validated")
            if preparation.request != request:
                raise _order_error()
            _PREPARATIONS[preparation] = "executing"
        elif (
            preparation is not None
            or request.operation not in CATALOG_CANDIDATE_ENTRIES
        ):
            raise _order_error()
        global_write = CATALOG_CANDIDATE_ENTRIES[request.operation].global_write
        tenant_id = (
            request.target_tenant_id
            if request.operation.startswith("model_policy.")
            else request.tenant.tenant_id
        )
        modes = {
            request.tenant.tenant_id: LockMode.SHARED,
            tenant_id: LockMode.EXCLUSIVE,
        }
        scope = (
            authorization_transactions.scope_global(
                db, budget=TransactionBudget(timeout_ms=60000)
            )
            if global_write
            else authorization_transactions.scope(
                db,
                tenants=tuple(TenantLock(key, mode) for key, mode in modes.items()),
                budget=TransactionBudget(
                    timeout_ms=60000
                    if request.operation.endswith("put_many")
                    else 15000
                ),
            )
        )
        new_id = next_id()
        with bounded_authorization_reads(db):
            async with scope as tx:
                identity = await self._identity(db, request)
                facts = await self._facts(db, request)
                if (
                    preparation is not None
                    and _facts_bytes(facts) != preparation.snapshot
                ):
                    raise stale()
                target = (
                    await read_policy_target(db, tenant_id=tenant_id)
                    if request.operation.startswith("model_policy.")
                    else None
                )
                evaluation = (
                    await self._delegation(db, request)
                    if request.operation == "role.agents"
                    else None
                )
                keys = set(identity.keys)
                for kind, rows in facts.items():
                    scoped = (
                        tenant_id
                        if kind
                        in {ResourceKind.ROLE_AGENT, ResourceKind.TENANT_MODEL_POLICY}
                        else None
                    )
                    keys.update(_key(kind, row, scoped) for row in rows)
                if target:
                    keys.update(target.keys)
                if evaluation:
                    for kind, ids in (
                        (ResourceKind.ROLE, evaluation.role_ids),
                        (ResourceKind.DEPT, evaluation.dept_ids),
                        (ResourceKind.USER, evaluation.user_ids),
                    ):
                        keys.update(
                            ResourceKey(kind, tenant_id, value) for value in ids
                        )
                new = self._new_keys(request, facts, new_id)
                if audit:
                    keys.update(audit.existing)
                    new += audit.new
                await tx.lock(LockPlan(existing=tuple(keys), new=new))
                await assert_identity(db, identity)
                if _facts_bytes(await self._facts(db, request)) != _facts_bytes(facts):
                    raise stale()
                if (
                    target
                    and await read_policy_target(db, tenant_id=tenant_id) != target
                ):
                    raise stale()
                if evaluation:
                    locked = await self._delegation(db, request)
                    if locked != evaluation:
                        raise stale()
                    expected = json.loads(request.expected_bytes)
                    if expected is not None and locked.snapshot != expected:
                        raise BusinessRuleException(
                            "角色 Agent 审批快照已变化",
                            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
                        )
                frame = (
                    tx.aggregate_global() if global_write else tx.aggregate(tenant_id)
                )
                async with frame:
                    result = await self._apply(
                        db, tx, request, facts, new_id, identity.user_name
                    )
                if audit:
                    changes = None
                    if request.operation == "agent.update":
                        before = _row(facts, ResourceKind.AGENT, request.target_id)

                        def safe(value):
                            return (
                                {"sha256": hashlib.sha256(value.encode()).hexdigest()}
                                if isinstance(value, str)
                                else value
                            )

                        changes = {
                            name: {"before": safe(before[name]), "after": safe(value)}
                            for name, value in request.payload().items()
                            if before[name] != value
                        }
                    await audit.append(
                        tx, result_summary={"recordCount": 1}, changes=changes
                    )
                return (
                    await self._http_result(db, request, result)
                    if http_result
                    else result
                )

    async def _http_result(self, db, request, result):
        """Freeze the existing public DTO while the complete plan is still held."""
        op = request.operation
        if op == "agent.update":
            row = await db.get(AiAgent, result["agent_id"], populate_existing=True)
            return AgentAdminDetailItem.model_validate(row)
        if op in {"provider.create", "provider.update"}:
            row = await db.get(
                AiProvider, result["provider_id"], populate_existing=True
            )
            return ProviderOut.from_record(row)
        if op in {"model.create", "model.update"}:
            row = await db.get(AiModel, result["model_id"], populate_existing=True)
            return ModelOut.model_validate(row)
        if op == "model_policy.put_many":
            return PlatformTenantModelCatalogOut.from_projection(result)
        if op == "model_policy.put":
            row = next(
                row for row in result.models if row.model_id == request.target_id
            )
            return PlatformTenantModelPolicyOut.from_projection(row)
        return result

    async def _apply(self, db, tx, request, facts, new_id, actor_name):
        op, data = request.operation, request.payload()
        if op == "agent.update":
            row = _row(facts, ResourceKind.AGENT, request.target_id)
            translations = dict(row["i18n_keys"] or {})
            from pydantic.alias_generators import to_camel  # noqa: PLC0415

            for name, value in data.items():
                if row[name] != value:
                    translations.pop(to_camel(name), None)
            if translations != (row["i18n_keys"] or {}):
                data["i18n_keys"] = translations
            await tx.update_held(_key(ResourceKind.AGENT, row), data)
            return {"agent_id": request.target_id}
        if op.startswith("provider.") or op.startswith("model."):
            kind = (
                ResourceKind.PROVIDER
                if op.startswith("provider.")
                else ResourceKind.MODEL
            )
            creating = op.endswith("create")
            row = None if creating else _row(facts, kind, request.target_id)
            if kind is ResourceKind.MODEL:
                _row(facts, ResourceKind.PROVIDER, request.provider_id)
                if row and row["provider_id"] != request.provider_id:
                    raise BusinessRuleException(
                        "模型不属于指定 Provider",
                        error_code="AI_PROVIDER_MODEL_MISMATCH",
                    )
            if op.endswith("delete"):
                model_ids = (
                    {
                        r["model_id"]
                        for r in facts[ResourceKind.MODEL]
                        if r["provider_id"] == request.target_id
                    }
                    if kind is ResourceKind.PROVIDER
                    else {request.target_id}
                )
                reference = select(TenantAiModelPolicy.model_id)
                if kind is ResourceKind.PROVIDER:
                    reference = reference.join(
                        AiModel, AiModel.model_id == TenantAiModelPolicy.model_id
                    ).where(AiModel.provider_id == request.target_id)
                else:
                    reference = reference.where(
                        TenantAiModelPolicy.model_id == request.target_id
                    )
                if await db.scalar(reference.limit(1)) is not None:
                    raise BusinessRuleException(
                        "目录已被租户模型策略引用，请先移除租户授权",
                        error_code="AI_PROVIDER_IN_USE_BY_TENANT_POLICY"
                        if kind is ResourceKind.PROVIDER
                        else "AI_MODEL_IN_USE_BY_TENANT_POLICY",
                    )
                if kind is ResourceKind.PROVIDER:
                    for model in facts[ResourceKind.MODEL]:
                        if model["model_id"] in model_ids:
                            await tx.delete_held(_key(ResourceKind.MODEL, model))
                await tx.delete_held(_key(kind, row))
                return {"deleted": request.target_id}
            unique = "provider_code" if kind is ResourceKind.PROVIDER else "name"
            value = data.get(unique, row[unique] if row else None)
            if any(
                other[unique] == value
                and (creating or other[_PK[kind]] != request.target_id)
                and (
                    kind is ResourceKind.PROVIDER
                    or other["provider_id"] == request.provider_id
                )
                for other in facts[kind]
            ):
                raise DuplicateException(
                    field="提供商标识" if kind is ResourceKind.PROVIDER else "模型名称",
                    value=value,
                )
            if kind is ResourceKind.PROVIDER and "api_key" in data:
                raw = data["api_key"]
                if not creating and (not raw or raw == decrypt_value(row["api_key"])):
                    data.pop("api_key")
                else:
                    data["api_key"] = encrypt_value(raw)
            if creating:
                data[_PK[kind]] = new_id
                data["create_by"] = actor_name
                if kind is ResourceKind.MODEL:
                    data["provider_id"] = request.provider_id
                await tx.insert_reserved(ResourceKey(kind, None, new_id), data)
            else:
                await tx.update_held(_key(kind, row), data)
            return {_PK[kind]: new_id if creating else request.target_id}
        if op == "role.agents":
            wanted = {int(value) for value in data["agent_ids"]}
            if not wanted <= {row["agent_id"] for row in facts[ResourceKind.AGENT]}:
                raise NotFoundException("AI Agent", error_code="AI_AGENT_NOT_FOUND")
            stored = {row["agent_id"]: row for row in facts[ResourceKind.ROLE_AGENT]}
            for aid, row in stored.items():
                key = _key(ResourceKind.ROLE_AGENT, row, request.tenant.tenant_id)
                if aid not in wanted:
                    await tx.delete_held(key)
                elif not row["enabled"]:
                    await tx.update_held(key, {"enabled": True})
            for aid in sorted(wanted - stored.keys()):
                await tx.insert_reserved(
                    ResourceKey(
                        ResourceKind.ROLE_AGENT,
                        request.tenant.tenant_id,
                        (request.target_id, aid),
                    ),
                    {
                        "tenant_id": request.tenant.tenant_id,
                        "role_id": request.target_id,
                        "agent_id": aid,
                        "enabled": True,
                    },
                )
            return {"role_id": request.target_id, "agent_ids": sorted(wanted)}
        return await self._policy(db, tx, request, facts)

    async def _policy(self, db, tx, request, facts):
        tenant_id, op, data = (
            request.target_tenant_id,
            request.operation,
            request.payload(),
        )
        stored = {
            row["model_id"]: row for row in facts[ResourceKind.TENANT_MODEL_POLICY]
        }
        if op.endswith("delete"):
            if request.target_id not in stored:
                raise NotFoundException(
                    "租户模型策略", error_code="PLATFORM_TENANT_MODEL_POLICY_NOT_FOUND"
                )
            await tx.delete_held(
                ResourceKey(
                    ResourceKind.TENANT_MODEL_POLICY, tenant_id, request.target_id
                )
            )
            return {"deleted": request.target_id}
        catalog = await tenant_model_policy_admin_service._catalog(db, tenant_id)
        existing = {row.model_id: row for row in catalog.models}
        if op.endswith("put_many"):
            items = [
                {**item, "model_id": int(item["model_id"])} for item in data["policies"]
            ]
            if data["revision"] != catalog.revision or {
                item["model_id"] for item in items
            } != set(existing):
                raise BusinessRuleException(
                    "模型或授权已变化，请重新加载后再保存",
                    error_code="PLATFORM_TENANT_MODEL_POLICIES_STALE",
                )
        else:
            items = [{**data, "model_id": request.target_id}]
        for item in items:
            old = existing.get(item["model_id"])
            if old is None:
                raise NotFoundException("AI模型", error_code="AI_MODEL_NOT_FOUND")
            if (
                item["enabled"]
                and not old.model_available
                and (
                    not op.endswith("put_many")
                    or not old.enabled
                    or item["is_default"]
                    or item["daily_quota_per_user"] != old.daily_quota_per_user
                )
            ):
                raise BusinessRuleException(
                    "所选模型当前不可用于租户",
                    error_code="PLATFORM_TENANT_MODEL_UNAVAILABLE",
                )
        wanted_default = next(
            (item["model_id"] for item in items if item["is_default"]), None
        )
        # Release the old partial-unique slot before inserting/updating the new one.
        for mid, row in stored.items():
            if (
                row["is_default"]
                and (op.endswith("put_many") or wanted_default is not None)
                and mid != wanted_default
            ):
                await tx.update_held(
                    ResourceKey(ResourceKind.TENANT_MODEL_POLICY, tenant_id, mid),
                    {"is_default": False},
                )
        for item in items:
            mid = item["model_id"]
            values = {
                key: item[key]
                for key in ("enabled", "is_default", "daily_quota_per_user")
            }
            key = ResourceKey(ResourceKind.TENANT_MODEL_POLICY, tenant_id, mid)
            if mid in stored:
                await tx.update_held(key, values)
            elif item["enabled"] or not op.endswith("put_many"):
                await tx.insert_reserved(
                    key, {"tenant_id": tenant_id, "model_id": mid, **values}
                )
        return await tenant_model_policy_admin_service._catalog(db, tenant_id)

    async def prepare_probe(self, db, **kwargs):
        if kwargs.get("operation") not in PROBE_OPERATIONS:
            raise _order_error()
        return await self.prepare(db, **kwargs)

    async def run_probe(self, preparation):
        _external(preparation, "read")
        if preparation.request.operation not in PROBE_OPERATIONS:
            raise _order_error()
        _PREPARATIONS[preparation] = "probing"
        from app.modules.ai.service.authorization_catalog_probe import (  # noqa: PLC0415
            probe,
        )

        await probe(json.loads(preparation.effective))
        result = hashlib.sha256(preparation.snapshot).hexdigest()
        _PREPARATIONS[preparation] = ("probed", result)
        return result

    async def complete_probe(self, db, *, preparation, result, audit):
        _external(preparation, ("probed", result))
        request = preparation.request
        _audit(audit, request)
        _fresh(db)
        _PREPARATIONS[preparation] = "completing"
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db,
                tenants=(TenantLock(request.tenant.tenant_id, LockMode.SHARED),),
                budget=TransactionBudget(timeout_ms=8000),
            ) as tx:
                identity = await self._identity(db, request)
                facts = await self._facts(db, request)
                if _facts_bytes(facts) != preparation.snapshot:
                    raise stale()
                keys = set(identity.keys) | set(audit.existing)
                for kind, rows in facts.items():
                    keys.update(_key(kind, row) for row in rows)
                await tx.lock(LockPlan(existing=tuple(keys), new=audit.new))
                await assert_identity(db, identity)
                if _facts_bytes(await self._facts(db, request)) != preparation.snapshot:
                    raise stale()
                await audit.append(tx, result_summary={"recordCount": 1})
                return {"status": "success"}


catalog_authorization_candidate = CatalogAuthorizationCandidate()
