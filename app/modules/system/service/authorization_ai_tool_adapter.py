"""Fixed System tool adapters for the opt-in planned Gateway owner."""

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult, UIResult
from app.modules.system.ai_tools.common import (
    _model_validate_for_ai,
    _validate_enable_status,
)
from app.modules.system.ai_tools.dept import _department_result
from app.modules.system.ai_tools.role import _role_data_scope_code, _role_result
from app.modules.system.ai_tools.user_assignment import (
    _format_ai_dept_assignments,
    _format_ai_roles,
    _parse_ai_dept_assignments,
    _parse_ai_role_ids,
)
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptMove, DeptUpdate
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as system,
)

TOOL_OPERATIONS = MappingProxyType(
    {
        "dept.create": "dept.create",
        "dept.update": "dept.update",
        "dept.move": "dept.move",
        "role.create": "role.create",
        "role.update": "role.update",
        "role.update_menus": "role.menus",
        "user.update_dept": "user.departments",
        "user.update_roles": "user.roles",
    }
)


def _invalid():
    return BusinessRuleException(
        "工具参数或审批快照无效", error_code="AI_PREPARED_ACTION_BINDING_INVALID"
    )


def _id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise _invalid()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class _ToolInput:
    tool: str
    tenant: object
    command: object
    snapshot: dict
    args_hash: str


class SystemAIToolAdapter:
    def validate_binding(
        self, command, *, tool, args, snapshot, tenant, actor_user_version
    ):
        if (
            type(command) is not _ToolInput
            or command.tool != tool
            or command.tenant != tenant
            or command.command.values[:2] != (tenant, actor_user_version)
            or command.args_hash != compute_args_hash(args)
            or command.snapshot != snapshot
        ):
            raise _invalid()

    async def prepare_input(
        self, db, *, tool, args, snapshot, tenant, actor_user_version
    ):
        if (
            tool not in TOOL_OPERATIONS
            or type(args) is not dict
            or type(snapshot) is not dict
        ):
            raise _invalid()
        values, snapshot = deepcopy(args), deepcopy(snapshot)
        target = None
        model = None
        if tool.startswith("dept."):
            if tool != "dept.create":
                target = _id(values.pop("dept_id", None))
            if tool == "dept.move":
                if set(values) != {"new_parent_id"}:
                    raise _invalid()
                parent = values["new_parent_id"]
                values["new_parent_id"] = (
                    str(_id(parent)) if parent is not None else None
                )
                model = DeptMove
            else:
                allowed = {
                    "dept_name",
                    "order_num",
                    "leader",
                    "phone",
                    "email",
                    "status",
                }
                if tool == "dept.create":
                    allowed.add("parent_id")
                    if "parent_id" not in values:
                        raise _invalid()
                    if values["parent_id"] is not None:
                        _id(values["parent_id"])
                    values.setdefault("status", "1")
                if set(values) - allowed:
                    raise _invalid()
                if "status" in values:
                    values["status"] = _validate_enable_status(values["status"])
                model = DeptCreate if tool == "dept.create" else DeptUpdate
        elif tool.startswith("role."):
            if tool != "role.create":
                target = _id(values.pop("role_id", None))
            if tool == "role.update_menus":
                if set(values) != {"menu_ids"} or type(values["menu_ids"]) is not list:
                    raise _invalid()
                payload = [_id(n) for n in values["menu_ids"]]
                if len(payload) != len(set(payload)):
                    raise _invalid()
            else:
                allowed = {"role_name", "role_desc", "data_scope", "status", "dept_ids"}
                if tool == "role.create":
                    allowed.add("role_code")
                    if not {"data_scope", "status"} <= values.keys():
                        raise _invalid()
                if set(values) - allowed:
                    raise _invalid()
                if "data_scope" in values:
                    values["data_scope"] = _role_data_scope_code(values["data_scope"])
                if "status" in values:
                    values["status"] = _validate_enable_status(values["status"])
                if values.get("dept_ids") is not None:
                    if type(values["dept_ids"]) is not list:
                        raise _invalid()
                    ids = [_id(n) for n in values["dept_ids"]]
                    if len(ids) != len(set(ids)):
                        raise _invalid()
                model = RoleCreate if tool == "role.create" else RoleUpdate
        else:
            target = _id(values.pop("user_id", None))
            field = "dept_assignments" if tool == "user.update_dept" else "role_ids"
            if set(values) != {field}:
                raise _invalid()
            payload = (
                _parse_ai_dept_assignments(values[field])
                if tool == "user.update_dept"
                else _parse_ai_role_ids(values[field])
            )
        if model is not None:
            payload = _model_validate_for_ai(
                model,
                values,
                message="工具参数无效",
                error_code="AI_PREPARED_ACTION_BINDING_INVALID",
            )
        command = await system.prepare_input(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation=TOOL_OPERATIONS[tool],
            target_id=target,
            payload=payload,
            expected_snapshot=snapshot,
        )
        return _ToolInput(tool, tenant, command, snapshot, compute_args_hash(args))

    async def plan(self, db, tx, command):
        if type(command) is not _ToolInput:
            raise _invalid()
        return await system.plan(db, tx, command.command)

    async def apply(self, db, tx, command, work):
        if (
            type(command) is not _ToolInput
            or work.command.values != command.command.values
        ):
            raise _invalid()
        changed = await system.apply(db, tx, work)
        tool, tenant = command.tool, command.tenant
        identifier = changed["id"]
        if tool.startswith("dept."):
            row = await db.scalar(
                select(Dept)
                .where(Dept.tenant_id == tenant.tenant_id, Dept.dept_id == identifier)
                .execution_options(populate_existing=True)
            )
            affected = tuple(
                int(n)
                for n in command.snapshot.get("facts", {}).get("userIds", [])
                if int(n) != tenant.actor_user_id
            )
            return _department_result(
                action=tool.split(".")[1], department=row, affected_user_ids=affected
            )
        if tool.startswith("role."):
            row = await db.scalar(
                select(Role)
                .where(Role.tenant_id == tenant.tenant_id, Role.role_id == identifier)
                .execution_options(populate_existing=True)
            )
            return _role_result(action=tool.split(".")[1], role=row)
        # The shared evaluator has already revalidated every displayed old/new fact.
        user = await db.scalar(
            select(User)
            .where(User.tenant_id == tenant.tenant_id, User.user_id == identifier)
            .execution_options(populate_existing=True)
        )
        snapshot = command.snapshot
        departments = tool == "user.update_dept"
        if departments:
            old = tuple(
                (int(n["deptId"]), n["isPrimary"]) for n in snapshot["oldAssignments"]
            )
            new = tuple(work.prepared["assignments"])
            previous, current = (
                _format_ai_dept_assignments(n, snapshot) for n in (old, new)
            )
            refs = (
                {"type": "dept", "id": str(n)}
                for n in sorted({d for d, _ in (*old, *new)})
            )
            old_ids, new_ids = [str(d) for d, _ in old], [str(d) for d, _ in new]
        else:
            old = tuple(int(n) for n in snapshot["target"]["roleIds"])
            new = tuple(work.prepared["roles"])
            previous, current = (_format_ai_roles(n, snapshot) for n in (old, new))
            refs = (
                {"type": "delegable_role", "id": str(n)}
                for n in sorted(set(old) | set(new))
            )
            old_ids, new_ids = [str(n) for n in old], [str(n) for n in new]
        label = "Departments" if departments else "Roles"
        kind = "dept" if departments else "role"
        extra = (
            ()
            if departments
            else ({"type": "complete_user_role_assignment", "id": str(identifier)},)
        )
        return ToolResult.success(
            data={
                "updated": 1,
                "userName": user.user_name,
                f"previous{label}": previous,
                f"new{label}": current,
            },
            projection=ResultProjection(
                subject_refs=({"type": "user", "id": str(identifier)}, *extra, *refs)
            ),
            ui=UIResult(
                view_type="detail_card",
                view_data={
                    "title": user.user_name,
                    "fields": [
                        {"label": f"page.ai.chat.previous{label}", "value": previous},
                        {"label": f"page.ai.chat.new{label}", "value": current},
                    ],
                },
                audit={
                    "affected_user_ids": [str(identifier)],
                    f"old_{kind}_ids": old_ids,
                    f"new_{kind}_ids": new_ids,
                },
                label_key=f"ai.tool.{tool}.result",
                label_params={"userName": user.user_name},
            ),
        )


system_ai_tool_adapter = SystemAIToolAdapter()
