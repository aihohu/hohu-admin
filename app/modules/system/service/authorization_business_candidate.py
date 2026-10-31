"""Fixed dictionary and scoped-demo commands under the tenant writer barrier."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.auth.service import load_live_user_authority
from app.modules.system.schemas.data_scope_demo import (
    DataScopeDemoCreate,
    DataScopeDemoUpdate,
)
from app.modules.system.schemas.dict_data import DictDataCreate, DictDataUpdate
from app.modules.system.schemas.dict_type import DictTypeCreate, DictTypeUpdate
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.data_scope_demo_service import data_scope_demo_service

_SURFACES = {
    "dict-type": (K.DICT_TYPE, DictTypeCreate, DictTypeUpdate),
    "dict-data": (K.DICT_DATA, DictDataCreate, DictDataUpdate),
    "data-scope-demo": (K.DATA_SCOPE_DEMO, DataScopeDemoCreate, DataScopeDemoUpdate),
}
_ACTIONS = {
    "create": "add",
    "update": "edit",
    "delete": "delete",
    "batch-delete": "batch-delete",
}


@dataclass(frozen=True)
class BusinessCommand:
    surface: str
    action: str
    ids: tuple
    values: dict
    new_id: int | None


def prepare(operation, payload, ids):
    if type(operation) is not str or operation.count(".") != 1:
        raise _order_error()
    surface, action = operation.split(".")
    if (
        surface not in _SURFACES
        or action not in _ACTIONS
        or (surface == "data-scope-demo" and action == "batch-delete")
    ):
        raise _order_error()
    if (
        type(ids) not in {list, tuple}
        or len(ids) > 256
        or any(type(value) is not int or not 0 < value < 2**63 for value in ids)
    ):
        raise _order_error()
    ids = tuple(sorted(set(ids)))
    if action in {"update", "delete"} and len(ids) != 1:
        raise _order_error()
    if surface == "dict-data" and action == "batch-delete" and not ids:
        raise InvalidParameterException("未选择要删除的字典数据")
    values = {}
    if action in {"create", "update"}:
        schema = _SURFACES[surface][1 if action == "create" else 2]
        values = schema.model_validate(payload).model_dump(
            exclude_unset=action == "update"
        )
    elif payload is not None:
        raise _order_error()
    if (
        values.get("content") is not None
        and len(values["content"].encode("utf-8")) > 256 * 1024
    ):
        raise BusinessRuleException(
            "内容超过事务预算", error_code="BUSINESS_INPUT_TOO_LARGE"
        )
    return BusinessCommand(
        surface, action, ids, values, next_id() if action == "create" else None
    )


class BusinessCandidate:
    async def discover(self, db, *, tenant, command):
        kinds = (
            (K.DATA_SCOPE_DEMO,)
            if command.surface == "data-scope-demo"
            else (K.DICT_TYPE, K.DICT_DATA)
        )
        result = {}
        for kind in kinds:
            spec = resource_spec(kind)
            stmt = select(spec.table).where(spec.table.c.tenant_id == tenant.tenant_id)
            if kind is K.DATA_SCOPE_DEMO:
                stmt = stmt.where(spec.table.c.demo_id.in_(command.ids))
            rows = [
                dict(row) for row in (await db.execute(stmt.limit(10001))).mappings()
            ]
            if len(rows) > 10000:
                raise BusinessRuleException(
                    "业务数据超过事务预算", error_code="BUSINESS_BATCH_TOO_LARGE"
                )
            result[kind] = sorted(rows, key=lambda row: row[spec.key_columns[0]])
        return result

    async def execute(self, db, tx, *, tenant, actor_user_version, command):
        if type(command) is not BusinessCommand:
            raise _order_error()
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=(f"system:{command.surface}:{_ACTIONS[command.action]}",),
        )
        facts = await self.discover(db, tenant=tenant, command=command)
        kind = _SURFACES[command.surface][0]
        spec = resource_spec(kind)
        key_name = spec.key_columns[0]
        new = (
            ResourceKey(kind, tenant.tenant_id, command.new_id)
            if command.new_id
            else None
        )
        keys = set(identity.keys)
        for entry, rows in facts.items():
            keys.update(
                ResourceKey(
                    entry, tenant.tenant_id, row[resource_spec(entry).key_columns[0]]
                )
                for row in rows
            )
        await tx.lock(
            LockPlan(existing=tuple(keys), new=(NewResource(new),) if new else ())
        )
        await assert_identity(db, identity)
        if facts != await self.discover(db, tenant=tenant, command=command):
            raise _order_error()
        values, action = dict(command.values), command.action
        targets = [row for row in facts[kind] if row[key_name] in command.ids]
        if action != "create" and {row[key_name] for row in targets} != set(
            command.ids
        ):
            raise NotFoundException("业务数据")
        if kind is K.DATA_SCOPE_DEMO:
            actor = await load_live_user_authority(db, tenant=tenant)
            if action == "create":
                department = await data_scope_demo_service._get_primary_dept_id(
                    db, actor.user_id, tenant=tenant
                )
                if department is None:
                    raise BusinessRuleException(
                        "当前用户未配置主部门，无法创建数据",
                        error_code="USER_NO_PRIMARY_DEPT",
                    )
                values |= {"dept_id": department, "create_by": actor.user_id}
            else:
                await data_scope_demo_service._get_visible(
                    db, command.ids[0], actor, tenant=tenant
                )
        elif kind is K.DICT_TYPE:
            if action == "create":
                for column in ("dict_type", "dict_name"):
                    if any(row[column] == values[column] for row in facts[kind]):
                        raise DuplicateException("字典类型", values[column])
            if action in {"delete", "batch-delete"} and any(
                row["dict_type"] in {target["dict_type"] for target in targets}
                for row in facts[K.DICT_DATA]
            ):
                raise BusinessRuleException("该字典类型下存在数据，请先删除数据")
        elif kind is K.DICT_DATA and (
            action == "create" or values.get("dict_type") is not None
        ):
            code = values["dict_type"]
            if not any(row["dict_type"] == code for row in facts[K.DICT_TYPE]):
                raise BusinessRuleException(f"字典类型 {code} 不存在")
        if action == "create":
            await tx.insert_reserved(new, values)
            identifier = command.new_id
        elif action == "update":
            identifier = command.ids[0]
            if values:
                await tx.update_factual(
                    ResourceKey(kind, tenant.tenant_id, identifier),
                    values,
                    expected=targets[0],
                )
        else:
            for row in targets:
                await tx.delete_business_fact(
                    ResourceKey(kind, tenant.tenant_id, row[key_name]), expected=row
                )
            return len(targets)
        return dict(
            (
                await db.execute(
                    select(spec.table).where(
                        spec.table.c.tenant_id == tenant.tenant_id,
                        spec.table.c[key_name] == identifier,
                    )
                )
            )
            .mappings()
            .one()
        )


business_candidate = BusinessCandidate()
