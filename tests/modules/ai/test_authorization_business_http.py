"""Remaining dictionary and scoped-demo writes cannot bypass the plan boundary."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert

from app.db.base import user_depts
from app.db.session import AsyncSessionLocal, engine
from app.modules.system.api import data_scope_demo as demo_api
from app.modules.system.api import dict_data as data_api
from app.modules.system.api import dict_type as type_api
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.schemas.data_scope_demo import DataScopeDemoCreate
from app.modules.system.schemas.dict_data import DictDataCreate
from app.modules.system.schemas.dict_type import DictTypeCreate
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor


@pytest.mark.parametrize("kind", ["type", "data", "demo"])
async def test_business_route_writes_have_plan_permit(completion_case, kind):
    marker = "plan_" + uuid4().hex[:12]
    writes = []
    async with AsyncSessionLocal() as db, db.begin():
        db.add(DictType(tenant_id=0, dict_name=marker, dict_type=marker, status="1"))
        if kind == "demo":
            await db.execute(
                insert(user_depts).values(
                    tenant_id=0,
                    user_id=completion_case.tenant.actor_user_id,
                    dept_id=completion_case.department_id,
                    is_primary="Y",
                )
            )

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    try:
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(db, tenant=completion_case.tenant)
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                if kind == "type":
                    await type_api.add(
                        DictTypeCreate(
                            dict_name=marker + "_new",
                            dict_type=marker + "_new",
                            status="1",
                        ),
                        db=db,
                        _current_user=actor,
                        tenant=completion_case.tenant,
                    )
                elif kind == "data":
                    await data_api.add(
                        DictDataCreate(
                            dict_type=marker,
                            dict_label="label",
                            dict_value="value",
                            dict_sort=1,
                            is_default="N",
                            status="1",
                        ),
                        db=db,
                        _current_user=actor,
                        tenant=completion_case.tenant,
                    )
                else:
                    response = await demo_api.add_data_scope_demo(
                        DataScopeDemoCreate(title=marker),
                        db=db,
                        current_user=actor,
                        tenant=completion_case.tenant,
                    )
                    assert response.data.create_by == actor.user_id
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(writes) == 1 and writes[0] is not None
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(DictData).where(
                    DictData.tenant_id == 0, DictData.dict_type == marker
                )
            )
            await db.execute(
                delete(DictType).where(
                    DictType.tenant_id == 0,
                    DictType.dict_type.in_((marker, marker + "_new")),
                )
            )
            await db.execute(
                delete(DataScopeDemo).where(
                    DataScopeDemo.tenant_id == 0, DataScopeDemo.title == marker
                )
            )
            if kind == "demo":
                await db.execute(
                    delete(user_depts).where(
                        user_depts.c.tenant_id == 0,
                        user_depts.c.user_id == completion_case.tenant.actor_user_id,
                        user_depts.c.dept_id == completion_case.department_id,
                    )
                )
