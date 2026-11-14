"""All delivery queries end their protected transaction before returning DTOs."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import rollback_authorization_session
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.delivery_log_read import log_page


class DeliveryReadOwner:
    def __init__(self, factory):
        self.factory = factory

    async def logs(self, *, tenant, actor_user_version, kind, query):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(
                    db,
                    AuthorizationReadBudget(
                        max_statements=512, max_rows=10000, max_bytes=16 * 1024 * 1024
                    ),
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        return await log_page(
                            db,
                            tx,
                            tenant=tenant,
                            version=actor_user_version,
                            kind=kind,
                            query=query,
                        )
            finally:
                await rollback_authorization_session(db)

    async def feedback(self, *, tenant, actor_user_version, query=None, days=None):
        from app.modules.ai.service.feedback_delivery import (  # noqa: PLC0415
            feedback_page,  # noqa: PLC0415
        )

        async with self.factory() as db:
            try:
                with bounded_authorization_reads(
                    db,
                    AuthorizationReadBudget(
                        max_statements=512, max_rows=10000, max_bytes=16 * 1024 * 1024
                    ),
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        return await feedback_page(
                            db,
                            tx,
                            tenant=tenant,
                            version=actor_user_version,
                            query=query,
                            days=days,
                        )
            finally:
                await rollback_authorization_session(db)

    async def agents(self, *, tenant, actor_user_version, with_sources=False):
        if type(with_sources) is not bool:
            from app.db.authorization_plan_session import _order_error  # noqa: PLC0415

            raise _order_error()
        from app.modules.ai.models.agent import AiAgent  # noqa: PLC0415
        from app.modules.system.field_policy.delivery_catalog import (  # noqa: PLC0415
            SORT,  # noqa: PLC0415
        )
        from app.modules.system.schemas.delivery_projection import (  # noqa: PLC0415
            ProjectedAgent,  # noqa: PLC0415
        )
        from app.modules.system.service.delivery_read_context import (  # noqa: PLC0415
            load_delivery_context,  # noqa: PLC0415
        )

        async with self.factory() as db:
            try:
                with bounded_authorization_reads(
                    db,
                    AuthorizationReadBudget(
                        max_statements=512, max_rows=10000, max_bytes=16 * 1024 * 1024
                    ),
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        context = await load_delivery_context(
                            db,
                            tx,
                            tenant=tenant,
                            version=actor_user_version,
                            action="delivery.agent-list",
                            agents=True,
                        )
                        await context.prove(
                            db,
                            "ai.agent",
                            AiAgent.__table__,
                            (("display_order", SORT),),
                        )
                        rows = await context.rows(
                            db,
                            "ai.agent",
                            AiAgent,
                            "agent_id",
                            order=(AiAgent.display_order, AiAgent.agent_id),
                            maximum=1025,
                        )
                        if len(rows) > 1024:
                            from app.core.field_policy.contracts import (  # noqa: PLC0415
                                limit,  # noqa: PLC0415
                            )

                            limit(False)
                        output = []
                        groups = []
                        for record, values, _ in rows:
                            if with_sources:
                                from app.modules.ai.service.delivery_source_capture import (  # noqa: PLC0415
                                    capture_domain,  # noqa: PLC0415
                                )

                                groups.append(
                                    await capture_domain(
                                        db,
                                        context,
                                        "ai.agent",
                                        records=(int(record.record_key),),
                                        readable=tuple(
                                            name
                                            for name in ("name", "description")
                                            if name in values
                                        ),
                                    )
                                )
                            values["agent_id"] = str(values["agent_id"])
                            values["field_policy"] = context.policy(record)
                            output.append(
                                ProjectedAgent.model_validate(values).model_dump(
                                    mode="json", by_alias=True
                                )
                            )
                        if not with_sources:
                            return output
                        from app.modules.ai.service.delivery_source_capture import (  # noqa: PLC0415
                            capture_domain,  # noqa: PLC0415
                        )
                        from app.modules.ai.service.field_authority_runtime import (  # noqa: PLC0415
                            ai_field_authority,  # noqa: PLC0415
                        )

                        groups.append(
                            await capture_domain(
                                db,
                                context,
                                "ai.agent",
                                queries=(("display_order", SORT),),
                            )
                        )
                        proof = await ai_field_authority.mint(
                            db, tx, tenant=tenant, domains=groups
                        )
                        return {"agents": output, "field_authority": proof}
            finally:
                await rollback_authorization_session(db)
