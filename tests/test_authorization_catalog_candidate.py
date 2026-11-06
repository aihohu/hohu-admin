"""Explicit AI catalog candidates preserve global and tenant authorization facts."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select, update

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.core.security import decrypt_value, encrypt_value
from app.core.tenant import TenantContext
from app.db.authorization_control_owner import control_candidate_owner
from app.db.base import role_menus
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.agent_admin import AgentAdminUpdateReq
from app.modules.ai.schemas.model import ModelCreate, ModelUpdate
from app.modules.ai.schemas.provider import (
    ProviderCreate,
    ProviderModelTestDraftRequest,
    ProviderUpdate,
)
from app.modules.ai.schemas.role_agent import RoleAgentBindReq
from app.modules.ai.service import authorization_catalog_probe as module
from app.modules.ai.service.authorization_catalog_candidate import (
    CATALOG_CANDIDATE_ENTRIES,
)
from app.modules.ai.service.authorization_catalog_candidate import (
    catalog_authorization_candidate as catalog,
)
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.platform.constants import PLATFORM_AI_WRITE
from app.modules.platform.schemas import (
    PlatformTenantModelPoliciesPut,
    PlatformTenantModelPolicyPut,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)

TENANT = TenantContext(0, "plan-0", 101, 1, "access_token")


@pytest.fixture
async def catalog_sessions(system_sessions, monkeypatch):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(AiProvider).values(
                provider_id=501,
                provider_code="openai",
                name="Provider",
                api_key=encrypt_value("private-test-key"),
                base_url="https://api.openai.com/v1",
                config={},
                is_enabled=True,
            )
        )
        await db.execute(
            insert(AiModel).values(
                model_id=601,
                provider_id=501,
                name="model-one",
                capabilities=["text"],
                config={},
                is_enabled=True,
            )
        )
        await db.execute(
            insert(AiModel).values(
                model_id=602,
                provider_id=501,
                name="model-two",
                capabilities=["text"],
                config={},
                is_enabled=True,
            )
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=701,
                code="user_mgmt",
                name="Users",
                description="A" * 50,
                system_prompt="private prompt",
                enabled=True,
                i18n_keys={"name": "builtin.name"},
            )
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=702,
                code="shared",
                name="Shared",
                description="A" * 50,
                enabled=True,
            )
        )
        await db.commit()

    async def dns(*_args, **_kwargs):
        return None

    monkeypatch.setattr(provider_egress, "validate_destination", dns)
    return system_sessions


async def audit_for(factory, operation, target_tenant_id=None):
    async with factory() as db:
        return await control_candidate_owner.authorize(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            permission=PLATFORM_AI_WRITE,
            reason="Verify catalog mutation",
            ticket_id="catalog-test",
            correlation_id=uuid4().hex,
            target_tenant_id=target_tenant_id,
            path="/platform/ai/catalog-candidate",
            method="PUT",
        )


def request(operation, **kwargs):
    return dict(tenant=TENANT, actor_user_version=1, operation=operation, **kwargs)


async def run(factory, operation, **kwargs):
    audit = (
        None
        if operation == "role.agents"
        else await audit_for(factory, operation, kwargs.get("target_tenant_id"))
    )
    async with factory() as db:
        values = request(operation, **kwargs)
        preparation = None
        if operation in {
            "provider.create",
            "provider.update",
            "model.create",
            "model.update",
        }:
            preparation = await catalog.prepare(db, **values)
            await db.rollback()
            await catalog.validate_preparation(preparation)
        try:
            result = await catalog.execute(
                db, audit=audit, preparation=preparation, **values
            )
            await db.commit()
            return result
        except BaseException:
            await db.rollback()
            raise


async def epochs(plain):
    async with plain() as db:
        global_epoch = await db.scalar(select(UserFieldPolicyGlobalGuard.epoch))
        tenant_epochs = dict(
            (
                await db.execute(
                    select(UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch)
                )
            ).all()
        )
    return global_epoch, tenant_epochs


def test_catalog_operations_are_explicit_and_remain_disconnected():
    assert set(CATALOG_CANDIDATE_ENTRIES) == {
        "agent.update",
        "provider.create",
        "provider.update",
        "provider.delete",
        "model.create",
        "model.update",
        "model.delete",
        "model_policy.put_many",
        "model_policy.put",
        "model_policy.delete",
        "role.agents",
    }
    assert all(
        not entry.production_connected for entry in CATALOG_CANDIDATE_ENTRIES.values()
    )


@pytest.mark.parametrize("field", ["agent_id", "code", "is_builtin"])
async def test_agent_identity_fields_rejected_before_any_database_access(field):
    value = {"agent_id": 1, "code": "immutable", "is_builtin": True}[field]
    with pytest.raises(BusinessException) as error:
        await catalog.execute(
            None,
            tenant=TENANT,
            actor_user_version=1,
            operation="agent.update",
            target_id=700,
            payload=AgentAdminUpdateReq(**{field: value}),
        )
    assert error.value.error_code == "AI_AGENT_IMMUTABLE_FIELD"


async def test_agent_change_all_tenants_once_then_noop_and_sanitized_result(
    catalog_sessions,
):
    factory, plain = catalog_sessions
    before = await epochs(plain)
    result = await run(
        factory,
        "agent.update",
        target_id=701,
        payload=AgentAdminUpdateReq(
            name="Changed", system_prompt="private changed prompt"
        ),
    )
    after = await epochs(plain)
    assert after == (
        before[0] + 1,
        {key: value + 1 for key, value in before[1].items()},
    )
    assert result == {"agent_id": 701}
    async with plain() as db:
        row = await db.get(AiAgent, 701)
        assert row.i18n_keys == {}
        logs = (await db.scalars(select(SysOperationLog.request_params))).all()
        assert all("private changed prompt" not in value for value in logs)
    await run(
        factory,
        "agent.update",
        target_id=701,
        payload=AgentAdminUpdateReq(
            name="Changed", system_prompt="private changed prompt"
        ),
    )
    assert await epochs(plain) == after


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (AgentAdminUpdateReq(description="short"), "AI_AGENT_DESC_LENGTH_INVALID"),
        (
            AgentAdminUpdateReq(model_preference="bad/url"),
            "AI_AGENT_MODEL_PREFERENCE_INVALID",
        ),
        (AgentAdminUpdateReq(daily_quota_per_user=0), "AI_AGENT_QUOTA_INVALID"),
    ],
)
async def test_agent_business_validation_keeps_error_codes(payload, code):
    with pytest.raises(BusinessException) as error:
        await catalog.execute(
            None, **request("agent.update", target_id=701, payload=payload)
        )
    assert error.value.error_code == code


async def test_provider_create_update_noop_credential_and_duplicate(catalog_sessions):
    factory, plain = catalog_sessions
    made = await run(
        factory,
        "provider.create",
        payload=ProviderCreate(
            provider_code="custom",
            name="New",
            api_key="new-secret",
            base_url="https://api.openai.com/v1",
        ),
    )
    pid = made["provider_id"]
    async with plain() as db:
        assert decrypt_value((await db.get(AiProvider, pid)).api_key) == "new-secret"
    before = await epochs(plain)
    await run(
        factory,
        "provider.update",
        target_id=pid,
        payload=ProviderUpdate(api_key="new-secret", name="New"),
    )
    await run(
        factory, "provider.update", target_id=pid, payload=ProviderUpdate(api_key="")
    )
    assert await epochs(plain) == before
    with pytest.raises(BusinessException):
        await run(
            factory,
            "provider.create",
            payload=ProviderCreate(
                provider_code="custom", name="Duplicate", api_key="x"
            ),
        )
    assert await epochs(plain) == before
    await run(
        factory,
        "provider.update",
        target_id=pid,
        payload=ProviderUpdate(api_key="replacement"),
    )
    async with plain() as db:
        assert decrypt_value((await db.get(AiProvider, pid)).api_key) == "replacement"


async def test_model_create_update_provider_path_and_duplicate(catalog_sessions):
    factory, plain = catalog_sessions
    made = await run(
        factory,
        "model.create",
        provider_id=501,
        payload=ModelCreate(name="new", capabilities=["text"]),
    )
    mid = made["model_id"]
    await run(
        factory,
        "model.update",
        target_id=mid,
        provider_id=501,
        payload=ModelUpdate(name="renamed", capabilities=["text", "vision"]),
    )
    async with plain() as db:
        assert (await db.get(AiModel, mid)).name == "renamed"
    with pytest.raises(BusinessException):
        await run(
            factory,
            "model.create",
            provider_id=501,
            payload=ModelCreate(name="renamed", capabilities=["text"]),
        )
    async with plain() as db:
        await db.execute(
            insert(AiProvider).values(
                provider_id=502,
                provider_code="other",
                name="Other",
                api_key="encrypted",
                base_url="https://api.openai.com",
            )
        )
        await db.commit()
    with pytest.raises(BusinessException) as error:
        await run(factory, "model.delete", target_id=mid, provider_id=502)
    assert error.value.error_code == "AI_PROVIDER_MODEL_MISMATCH"


@pytest.mark.parametrize("operation", ["provider.delete", "model.delete"])
async def test_policy_reference_blocks_global_delete_atomically(
    catalog_sessions, operation
):
    factory, plain = catalog_sessions
    async with plain() as db:
        await db.execute(
            insert(TenantAiModelPolicy).values(
                tenant_id=1, model_id=601, enabled=True, is_default=True
            )
        )
        await db.commit()
    before = await epochs(plain)
    kwargs = (
        {"target_id": 501}
        if operation == "provider.delete"
        else {"target_id": 601, "provider_id": 501}
    )
    with pytest.raises(BusinessException) as error:
        await run(factory, operation, **kwargs)
    assert error.value.error_code.endswith("IN_USE_BY_TENANT_POLICY")
    assert await epochs(plain) == before
    async with plain() as db:
        assert await db.get(AiProvider, 501) is not None
        assert await db.get(AiModel, 601) is not None


async def test_provider_delete_explicit_child_dml_precedes_parent(catalog_sessions):
    factory, plain = catalog_sessions
    statements = []

    def record(_c, _cursor, sql, _params, _ctx, _many):
        if sql.startswith("DELETE"):
            statements.append(sql)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        await run(factory, "provider.delete", target_id=501)
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    assert len([sql for sql in statements if "ai_model" in sql]) == 2
    assert "ai_provider" in statements[-1]


async def test_policy_single_default_switch_target_epoch_only(catalog_sessions):
    factory, plain = catalog_sessions
    before = await epochs(plain)
    await run(
        factory,
        "model_policy.put",
        target_tenant_id=1,
        target_id=601,
        payload=PlatformTenantModelPolicyPut(enabled=True, is_default=True),
    )
    await run(
        factory,
        "model_policy.put",
        target_tenant_id=1,
        target_id=602,
        payload=PlatformTenantModelPolicyPut(enabled=True, is_default=True),
    )
    after = await epochs(plain)
    assert after == (before[0], {0: before[1][0], 1: before[1][1] + 2})
    async with plain() as db:
        assert (await db.get(TenantAiModelPolicy, (1, 601))).is_default is False
        assert (await db.get(TenantAiModelPolicy, (1, 602))).is_default is True
    await run(
        factory,
        "model_policy.put",
        target_tenant_id=1,
        target_id=602,
        payload=PlatformTenantModelPolicyPut(enabled=True, is_default=True),
    )
    assert await epochs(plain) == after
    await run(factory, "model_policy.delete", target_tenant_id=1, target_id=602)
    async with plain() as db:
        assert await db.get(TenantAiModelPolicy, (1, 602)) is None


async def batch_payload(plain, **changes):
    async with plain() as db:
        snapshot = await tenant_model_policy_admin_service._catalog(db, 1)
    return PlatformTenantModelPoliciesPut(
        revision=snapshot.revision,
        policies=[
            {
                "model_id": str(row.model_id),
                "enabled": changes.get("enabled", False),
                "is_default": changes.get("enabled", False) and row.model_id == 601,
                "daily_quota_per_user": None,
            }
            for row in snapshot.models
        ],
    )


async def test_policy_batch_full_revision_noop_missing_id_and_unavailable(
    catalog_sessions,
):
    factory, plain = catalog_sessions
    payload = await batch_payload(plain)
    before = await epochs(plain)
    await run(factory, "model_policy.put_many", target_tenant_id=1, payload=payload)
    assert await epochs(plain) == before
    payload = await batch_payload(plain, enabled=True)
    await run(factory, "model_policy.put_many", target_tenant_id=1, payload=payload)
    after = await epochs(plain)
    with pytest.raises(BusinessException) as error:
        await run(factory, "model_policy.put_many", target_tenant_id=1, payload=payload)
    assert error.value.error_code == "PLATFORM_TENANT_MODEL_POLICIES_STALE"
    async with plain() as db:
        await db.execute(
            update(AiModel).where(AiModel.model_id == 601).values(is_enabled=False)
        )
        await db.commit()
    with pytest.raises(BusinessException) as error:
        await run(
            factory,
            "model_policy.put",
            target_tenant_id=1,
            target_id=601,
            payload=PlatformTenantModelPolicyPut(enabled=True, is_default=True),
        )
    assert error.value.error_code == "PLATFORM_TENANT_MODEL_UNAVAILABLE"
    assert await epochs(plain) == after


async def test_role_agents_delta_soft_enable_noop_and_stale_snapshot(catalog_sessions):
    factory, plain = catalog_sessions
    async with plain() as db:
        await db.execute(
            insert(RoleAiAgent).values(
                tenant_id=0, role_id=201, agent_id=701, enabled=False
            )
        )
        await db.commit()
    before = await epochs(plain)
    await run(
        factory,
        "role.agents",
        target_id=201,
        payload=RoleAgentBindReq(agent_ids=["701", "702"]),
    )
    after = await epochs(plain)
    assert after == (before[0], {0: before[1][0] + 1, 1: before[1][1]})
    await run(
        factory,
        "role.agents",
        target_id=201,
        payload=RoleAgentBindReq(agent_ids=["702", "701"]),
    )
    assert await epochs(plain) == after
    with pytest.raises(BusinessException) as error:
        await run(
            factory,
            "role.agents",
            target_id=201,
            payload=RoleAgentBindReq(agent_ids=[]),
            expected_snapshot={"bad": "snapshot"},
        )
    assert error.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    await run(
        factory,
        "role.agents",
        target_id=201,
        payload=RoleAgentBindReq(agent_ids=["702"]),
    )
    async with plain() as db:
        assert set((await db.scalars(select(RoleAiAgent.agent_id))).all()) == {702}


async def test_dns_outside_transaction_and_preparation_binds_input(
    catalog_sessions, monkeypatch
):
    factory, plain = catalog_sessions
    async with factory() as db:
        args = request(
            "provider.update", target_id=501, payload=ProviderUpdate(name="new")
        )
        prep = await catalog.prepare(db, **args)
        called = []

        async def dns(*_args):
            assert not db.in_transaction()
            called.append(True)

        monkeypatch.setattr(provider_egress, "validate_destination", dns)
        with pytest.raises(BusinessException):
            await catalog.validate_preparation(prep)
        assert not called
        await db.rollback()
        await catalog.validate_preparation(prep)
        audit = await audit_for(factory, "provider.update")
        args["payload"].name = "mutated"
        with pytest.raises(BusinessException):
            await catalog.execute(db, audit=audit, preparation=prep, **args)
    async with plain() as db:
        assert (await db.get(AiProvider, 501)).name == "Provider"


async def test_prepare_config_drift_and_fake_copy_rejected(catalog_sessions):
    factory, plain = catalog_sessions
    audit = await audit_for(factory, "model.update")
    args = request(
        "model.update", target_id=601, provider_id=501, payload=ModelUpdate(name="new")
    )
    async with factory() as db:
        prep = await catalog.prepare(db, **args)
        await db.rollback()
        with pytest.raises(BusinessException):
            await catalog.validate_preparation(replace(prep))
        await catalog.validate_preparation(prep)
        async with plain() as other:
            await other.execute(
                update(AiProvider)
                .where(AiProvider.provider_id == 501)
                .values(base_url="https://changed.example/v1")
            )
            await other.commit()
        with pytest.raises(BusinessException) as error:
            await catalog.execute(db, audit=audit, preparation=prep, **args)
        assert error.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
        await db.rollback()


async def test_audit_target_operation_and_actor_revocation(catalog_sessions):
    factory, plain = catalog_sessions
    wrong = await audit_for(factory, "model_policy.put", 1)
    async with factory() as db:
        with pytest.raises(BusinessException):
            await catalog.execute(
                db,
                audit=wrong,
                **request(
                    "agent.update",
                    target_id=701,
                    payload=AgentAdminUpdateReq(enabled=False),
                ),
            )
    right = await audit_for(factory, "agent.update")
    async with plain() as db:
        await db.execute(update(User).where(User.user_id == 101).values(status="2"))
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException):
            await catalog.execute(
                db,
                audit=right,
                **request(
                    "agent.update",
                    target_id=701,
                    payload=AgentAdminUpdateReq(enabled=False),
                ),
            )
        await db.rollback()
    async with plain() as db:
        assert (await db.get(AiAgent, 701)).enabled


async def test_audit_failure_rolls_back_global_business_and_epoch(
    catalog_sessions, monkeypatch
):
    factory, plain = catalog_sessions
    from app.modules.system.service.authorization_control_identity import (  # noqa: PLC0415
        ControlAudit,
    )

    before = await epochs(plain)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(ControlAudit, "append", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await run(
            factory,
            "agent.update",
            target_id=701,
            payload=AgentAdminUpdateReq(enabled=False),
        )
    assert await epochs(plain) == before
    async with plain() as db:
        assert (await db.get(AiAgent, 701)).enabled


@pytest.mark.parametrize("mode", ["success", "drift", "cancel"])
async def test_probe_release_network_recheck_noepoch_no_repeat(
    catalog_sessions, monkeypatch, mode
):
    factory, plain = catalog_sessions
    from app.modules.ai.service import authorization_catalog_probe  # noqa: PLC0415

    audit = await audit_for(factory, "provider.probe")
    before = await epochs(plain)
    async with factory() as db:
        prep = await catalog.prepare_probe(
            db, **request("provider.probe", target_id=601, provider_id=501)
        )
        await db.rollback()
        calls = []

        async def fake_probe(effective):
            assert not db.in_transaction()
            assert effective["api_key"] == "private-test-key"
            calls.append(True)
            if mode == "cancel":
                raise asyncio.CancelledError()

        monkeypatch.setattr(authorization_catalog_probe, "probe", fake_probe)
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await catalog.run_probe(prep)
        else:
            result = await catalog.run_probe(prep)
            if mode == "drift":
                async with plain() as other:
                    await other.execute(
                        update(AiModel)
                        .where(AiModel.model_id == 601)
                        .values(name="changed")
                    )
                    await other.commit()
                with pytest.raises(BusinessException):
                    await catalog.complete_probe(
                        db, preparation=prep, result=result, audit=audit
                    )
                await db.rollback()
            else:
                assert await catalog.complete_probe(
                    db, preparation=prep, result=result, audit=audit
                ) == {"status": "success"}
                await db.commit()
        with pytest.raises(BusinessException):
            await catalog.run_probe(prep)
        assert len(calls) == 1
    assert await epochs(plain) == before


@pytest.mark.parametrize(
    "kind", ["json_size", "json_depth", "capabilities", "role_ids"]
)
async def test_input_budget_rejected_before_session(kind):
    if kind == "json_size":
        op, payload, kwargs = (
            "provider.update",
            ProviderUpdate(config={"data": "x" * 65536}),
            {"target_id": 501},
        )
    elif kind == "json_depth":
        value = {}
        for _ in range(10):
            value = {"nested": value}
        op, payload, kwargs = (
            "provider.update",
            ProviderUpdate(config=value),
            {"target_id": 501},
        )
    elif kind == "capabilities":
        op, payload, kwargs = (
            "model.update",
            ModelUpdate(capabilities=["text"] * 33),
            {"target_id": 601, "provider_id": 501},
        )
    else:
        op, payload, kwargs = (
            "role.agents",
            RoleAgentBindReq(agent_ids=[str(n) for n in range(1, 258)]),
            {"target_id": 201},
        )
    with pytest.raises(BusinessException) as error:
        await catalog.execute(None, **request(op, payload=payload, **kwargs))
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"


async def test_stored_provider_config_budget_precedes_dns(catalog_sessions):
    factory, plain = catalog_sessions
    async with plain() as setup:
        await setup.execute(
            update(AiProvider)
            .where(AiProvider.provider_id == 501)
            .values(config={"data": "x" * 65536})
        )
        await setup.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await catalog.prepare(
                db,
                **request(
                    "model.update",
                    provider_id=501,
                    target_id=601,
                    payload=ModelUpdate(name="new"),
                ),
            )
        assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        await db.rollback()


async def test_probe_draft_validates_generation_before_network(catalog_sessions):
    factory, _ = catalog_sessions
    payload = ProviderModelTestDraftRequest(
        provider_id="501",
        provider_code="openai",
        model=ModelCreate(
            name="draft",
            capabilities=["text"],
            config={"generation": {"max_tokens": 0}},
        ),
    )
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await catalog.prepare_probe(
                db, **request("provider.probe_draft", payload=payload)
            )
        assert error.value.error_code == "AI_MODEL_GENERATION_INVALID"
        await db.rollback()


async def test_large_frozen_approval_snapshot_does_not_use_config_node_budget(
    catalog_sessions,
):
    factory, _ = catalog_sessions
    # These are ordinary fixed IDs, not a Provider configuration document.
    snapshot = {"ids": list(range(1100))}
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await catalog.execute(
                db,
                **request(
                    "role.agents",
                    target_id=201,
                    payload=RoleAgentBindReq(agent_ids=[]),
                    expected_snapshot=snapshot,
                ),
            )
        assert error.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
        await db.rollback()


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (201, "AI_ROLE_SELF_MUTATION_FORBIDDEN"),
        (301, "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"),
    ],
)
async def test_role_agent_preserves_nonadmin_delegation_boundaries(
    catalog_sessions, target, expected
):
    factory, plain = catalog_sessions
    async with plain() as setup:
        await setup.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_OWNER")
        )
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Target",
                role_code="R_TARGET",
                status="1",
                data_scope="5",
            )
        )
        await setup.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9001,
                menu_name="Agent binding",
                menu_type="F",
                status="1",
                permission="system:role:ai-agent-auth",
            )
        )
        await setup.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9001)
        )
        await setup.execute(
            insert(RoleAiAgent).values(
                tenant_id=0, role_id=201, agent_id=701, enabled=True
            )
        )
        await setup.commit()
    before = await epochs(plain)
    with pytest.raises(BusinessException) as error:
        await run(
            factory,
            "role.agents",
            target_id=target,
            payload=RoleAgentBindReq(agent_ids=["702"]),
        )
    assert error.value.error_code == expected
    assert await epochs(plain) == before


async def test_global_writer_waits_then_rechecks_revoked_actor(catalog_sessions):
    factory, plain = catalog_sessions
    audit = await audit_for(factory, "agent.update")
    attempted = asyncio.Event()

    def record(_c, _cursor, sql, _params, _context, _many):
        if "sys_user_field_policy_global_guard" in sql and "FOR UPDATE" in sql:
            attempted.set()

    async def contender():
        async with factory() as db:
            try:
                await catalog.execute(
                    db,
                    audit=audit,
                    **request(
                        "agent.update",
                        target_id=701,
                        payload=AgentAdminUpdateReq(enabled=False),
                    ),
                )
            finally:
                await db.rollback()

    task = None
    async with factory() as revoker:
        async with authorization_transactions.scope(
            revoker, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            key = ResourceKey(ResourceKind.USER, 0, 101)
            await tx.lock(LockPlan(existing=(key,)))
            event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
            try:
                task = asyncio.create_task(contender())
                await asyncio.wait_for(attempted.wait(), 2)
                assert not task.done()
                async with tx.aggregate(0):
                    await tx.update_held(key, {"status": "2"})
            finally:
                event.remove(
                    plain.kw["bind"].sync_engine, "before_cursor_execute", record
                )
        await revoker.commit()
    try:
        with pytest.raises(BusinessException) as error:
            await asyncio.wait_for(task, 3)
        assert error.value.error_code == "INVALID_CREDENTIALS"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with plain() as observer:
        assert (await observer.get(AiAgent, 701)).enabled


async def test_probe_draft_reads_secret_without_exposing_it(
    catalog_sessions, monkeypatch
):
    from app.modules.ai.service import authorization_catalog_probe  # noqa: PLC0415

    factory, plain = catalog_sessions
    audit = await audit_for(factory, "provider.probe_draft")
    payload = ProviderModelTestDraftRequest(
        provider_id="501",
        provider_code="openai",
        model=ModelCreate(name="draft", capabilities=["text"]),
    )
    before = await epochs(plain)
    async with factory() as db:
        prep = await catalog.prepare_probe(
            db, **request("provider.probe_draft", payload=payload)
        )
        assert "private-test-key" not in repr(prep)
        await db.rollback()

        async def probe(effective):
            assert effective["api_key"] == "private-test-key"
            assert effective["model"]["name"] == "draft"

        monkeypatch.setattr(authorization_catalog_probe, "probe", probe)
        result = await catalog.run_probe(prep)
        assert await catalog.complete_probe(
            db, preparation=prep, result=result, audit=audit
        ) == {"status": "success"}
        await db.commit()
    assert await epochs(plain) == before


async def test_real_probe_builds_private_sdk_no_retry_or_shared_client(monkeypatch):

    calls = {}

    class Client:
        async def __aenter__(self):
            calls["entered"] = True
            return self

        async def __aexit__(self, *_args):
            calls["closed"] = True

    def sdk(**kwargs):
        calls["sdk"] = kwargs
        return object()

    class Agent:
        def __init__(self, _model, **kwargs):
            calls["agent"] = kwargs

        async def run(self, prompt, **kwargs):
            calls["run"] = (prompt, kwargs)

    async def dns(*_args):
        return None

    monkeypatch.setattr(module, "build_provider_http_client", Client)
    monkeypatch.setattr(module, "AsyncOpenAI", sdk)
    monkeypatch.setattr(module, "OpenAIProvider", lambda **_kwargs: object())
    monkeypatch.setattr(module, "OpenAIChatModel", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "Agent", Agent)
    monkeypatch.setattr(provider_egress, "validate_destination", dns)
    await module.probe(
        {
            "provider_code": "openai",
            "base_url": "https://api.openai.com/v1",
            "config": {},
            "api_key": "secret",
            "model": {"name": "draft", "config": {}},
        }
    )
    assert calls["sdk"]["max_retries"] == 0
    assert calls["agent"]["retries"] == 0
    assert calls["run"][1]["model_settings"] == {"max_tokens": 16}
    assert calls["run"][1]["usage_limits"].request_limit == 1
    assert calls["closed"]
