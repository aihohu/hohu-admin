"""Fixed catalog projections; DNS inputs leave the database as plain values."""

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import func, select

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.config_projection import redact_url
from app.modules.ai.schemas.model import ModelOut
from app.modules.ai.schemas.provider import ProviderOut, ProviderQuery
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.ai.service.authorization_catalog_inputs import wire
from app.modules.ai.service.model_service import model_service
from app.modules.ai.service.provider_service import provider_service
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.platform.schemas import (
    PlatformTenantModelCatalogOut,
    PlatformTenantModelPolicyOut,
    PlatformTenantOut,
    PlatformTenantQuery,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.tenant_lifecycle_service import tenant_lifecycle_service
from app.utils.pagination import build_filters

READ_OPERATIONS = frozenset(
    {
        "agent.list",
        "agent.get",
        "model.options",
        "provider.list",
        "model.available",
        "model.list",
        "model_policy.list",
        "model_policy.catalog",
        "tenant.list",
        "tenant.get",
    }
)


def _json(value):
    return value.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True, slots=True, repr=False)
class CatalogProjection:
    data: object
    probes: tuple
    locations: tuple
    count: int

    def snapshot(self):
        return wire([self.data, self.probes, self.locations])

    def checked(self, checks):
        if len(checks) != len(self.locations):
            raise _order_error()
        data = deepcopy(self.data)
        for location, allowed in zip(self.locations, checks, strict=True):
            target = data
            for part in location:
                target = target[part]
            target["egressStatus"] = None if allowed else "EGRESS_POLICY_BLOCKED"
        return data


class CatalogReadCandidate:
    async def project(
        self,
        db,
        tx,
        *,
        operation,
        platform,
        payload=None,
        target_id=None,
        provider_id=None,
    ):
        tx.assert_session(db)
        if operation not in READ_OPERATIONS:
            raise _order_error()
        payload = payload or {}
        probes, locations = [], []

        def probe(provider, model, location):
            probes.append(
                deepcopy(
                    {
                        "provider_code": provider.provider_code,
                        "provider_base_url": provider.base_url,
                        "model_base_url": model.base_url if model else None,
                        "configs": (provider.config, model.config)
                        if model
                        else (provider.config,),
                    }
                )
            )
            locations.append(location)
            if len(probes) > 512:
                raise BusinessException(
                    422,
                    "模型目录超过网络检查预算",
                    error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
                )

        if operation == "agent.list":
            data = [
                _json(row)
                for row in await agent_admin_service.list_agents(db, platform=platform)
            ]
        elif operation == "agent.get":
            data = _json(
                await agent_admin_service.get_agent(db, target_id, platform=platform)
            )
        elif operation == "model.options":
            data = [
                _json(row)
                for row in await model_service.list_options(db, platform=platform)
            ]
        elif operation == "provider.list":
            query = ProviderQuery.model_validate(payload)
            filters = build_filters(
                AiProvider,
                {
                    "provider_code": "provider_code",
                    "name": ("name", "contains"),
                    "is_enabled": "is_enabled",
                },
                **query.model_dump(),
            )
            total = await db.scalar(
                select(func.pg_catalog.count(AiProvider.provider_id)).where(*filters)
            )
            rows = (
                await db.scalars(
                    select(AiProvider)
                    .where(*filters)
                    .order_by(AiProvider.provider_id)
                    .offset((query.current - 1) * query.size)
                    .limit(query.size)
                )
            ).all()
            data = {
                "records": [_json(ProviderOut.from_record(row)) for row in rows],
                "total": total,
                "current": query.current,
                "size": query.size,
            }
            for index, row in enumerate(rows):
                probe(row, None, ("records", index))
        elif operation in {"model.available", "model.list"}:
            if operation == "model.list":
                provider = await provider_service.get_by_id(
                    db, provider_id, platform=platform
                )
                pairs = [
                    (model, provider)
                    for model in await model_service.get_by_provider(
                        db, provider_id, platform=platform
                    )
                ]
            else:
                pairs = await model_service.list_available_with_provider(
                    db, payload.get("capability"), platform=platform
                )
            data = []
            for model, provider in pairs:
                data.append(
                    _json(ModelOut.model_validate(model))
                    if operation == "model.list"
                    else {
                        "modelId": str(model.model_id),
                        "providerId": str(provider.provider_id),
                        "providerCode": provider.provider_code,
                        "providerName": provider.name,
                        "model": model.name,
                        "capabilities": list(model.capabilities or []),
                        "baseUrl": redact_url(model.base_url or provider.base_url),
                        "egressStatus": None,
                    }
                )
                probe(provider, model, (len(data) - 1,))
        elif operation == "model_policy.list":
            rows = await tenant_model_policy_admin_service.list(
                db, tenant_id=platform.target_tenant_id, platform=platform
            )
            data = [
                _json(PlatformTenantModelPolicyOut.from_projection(row)) for row in rows
            ]
        elif operation == "model_policy.catalog":
            result = await tenant_model_policy_admin_service.catalog(
                db, tenant_id=platform.target_tenant_id, platform=platform
            )
            data = _json(PlatformTenantModelCatalogOut.from_projection(result))
        elif operation == "tenant.get":
            row = await tenant_lifecycle_service.get_tenant(
                db, tenant_id=platform.target_tenant_id, platform=platform
            )
            data = _json(PlatformTenantOut.from_record(row))
        else:
            query = PlatformTenantQuery.model_validate(payload)
            total = await db.scalar(select(func.pg_catalog.count(Tenant.tenant_id)))
            rows = (
                await db.scalars(
                    select(Tenant)
                    .order_by(Tenant.tenant_id.desc())
                    .offset((query.current - 1) * query.size)
                    .limit(query.size)
                )
            ).all()
            data = {
                "records": [_json(PlatformTenantOut.from_record(row)) for row in rows],
                "total": total,
                "current": query.current,
                "size": query.size,
            }
        records = (
            data if isinstance(data, list) else data.get("records", data.get("models"))
        )
        return CatalogProjection(
            data,
            tuple(probes),
            tuple(locations),
            len(records) if records is not None else 1,
        )


catalog_read_candidate = CatalogReadCandidate()
