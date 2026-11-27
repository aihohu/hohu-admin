"""Independent list-policy reader used by the multi-process integration test."""

import asyncio
import json
import re
import sys

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.session import engine
from app.modules.system.field_policy.ordinary_catalog import relation_catalog
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service,
)


async def main(schema):
    assert re.fullmatch(r"ufp_v0202a_[0-9a-f]{32}", schema)
    local = create_async_engine(
        engine.url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(local, class_=AuthorizationPlanSession)
    loads = 0

    @event.listens_for(local.sync_engine, "before_cursor_execute")
    def capture(_conn, _cursor, sql, *_args):
        nonlocal loads
        if "FROM sys_field_policy_manifest WHERE" in " ".join(sql.split()):
            loads += 1

    try:
        for line in sys.stdin:
            tenant = int(line)
            before = loads
            async with sessions() as db:
                try:
                    async with authorization_transactions.scope(
                        db, tenants=(TenantLock(tenant),)
                    ) as tx:
                        grants = await generic_field_policy_service.load_active_objects(
                            db,
                            tx,
                            tenant_id=tenant,
                            catalog=relation_catalog(),
                            object_ids={"system.user", "system.role", "system.dept"},
                            role_ids=(str(201 + tenant),),
                            reuse_list_snapshot=True,
                        )
                        result = [
                            g.ref.field_id
                            for g in grants
                            if g.ref.object_id == "system.role"
                        ]
                except BusinessException as exc:
                    result = exc.error_code
            sys.stdout.write(json.dumps([result, loads - before]) + "\n")
            sys.stdout.flush()
    finally:
        await local.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
