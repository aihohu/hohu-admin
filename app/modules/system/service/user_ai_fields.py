"""Field proof and projection for the closed builtin user AI surface."""

from dataclasses import replace

from sqlalchemy import or_, select

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.ai_plan import ai_query_plan, ai_write_fields
from app.modules.system.field_policy.evaluator import (
    project_user_values,
    resolve_record_fields,
    validate_write_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.user import User
from app.modules.system.service.user_field_compatibility import (
    require_legacy_field_policy,
)
from app.modules.system.service.user_field_context import (
    load_current_user,
    user_field_context,
)
from app.modules.system.service.user_query_coverage import assert_user_query_coverage
from app.modules.system.service.user_read_service import _legacy_rules
from app.utils.data_scope import resolve_data_scope_for_roles

SUPPORTED = frozenset(
    {
        "user.list",
        "user.lookup",
        "user.count",
        "user.stats",
        "user.distinct",
        "user.create",
        "user.update",
        "user.reset_password",
        "user.batch_delete",
        "user.update_roles",
        "user.update_dept",
    }
)
_LABELS = {
    "userName": "user_name",
    "nickname": "nickname",
    "userPhone": "user_phone",
    "userEmail": "user_email",
    "userGender": "user_gender",
    "userStatus": "status",
}


def _denied(code="FIELD_WRITE_DENIED"):
    return AuthorizationException("所需字段未获授权", error_code=code)


class UserAIFields:
    async def context(self, db, tx, *, tenant, version):
        # The AI owner first proves the never-enforced legacy/ready gate. This
        # compatibility provider is not an active-policy loader.
        await require_legacy_field_policy(
            db, tx, tenant_id=tenant.tenant_id, _after_plan=True
        )
        actor = await load_current_user(db, tenant=tenant, actor_user_version=version)
        return await user_field_context.load(
            db,
            tx,
            tenant=tenant,
            actor_user_version=version,
            grants=_legacy_rules(
                tuple(str(r.role_id) for r in actor.roles if r.status == "1")
            ),
            _after_plan=True,
        )

    async def decisions(
        self, db, tx, context, ids, operation="read", purpose="management.read"
    ):
        return await context.decisions(
            db,
            tx,
            user_ids=tuple(sorted(set(ids))),
            operation=operation,
            purpose=purpose,
            entrypoint="ai-tool",
            _after_plan=True,
        )

    async def before(self, db, tx, *, tenant, version, tool, args, preflight):
        if tool not in SUPPORTED:
            return None
        context = await self.context(db, tx, tenant=tenant, version=version)
        await assert_user_query_coverage(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            base_predicates=context.base_predicates,
            plan=ai_query_plan(tool, args),
            grants=context.grants,
            role_scopes=context.role_scopes,
            _after_plan=True,
        )
        if not preflight:
            return context
        requirements = ai_write_fields(tool, args)
        for name in ("user_email", "user_phone"):
            if isinstance(args.get(name), str) and "*" in args[name]:
                raise _denied("FIELD_MASKED_INPUT_INVALID")
        ids = []
        if tool == "user.create":
            actor = await load_current_user(
                db, tenant=tenant, actor_user_version=version
            )
            roles = []
            dept_id = args.get("primary_dept_id")
            for role in actor.roles:
                if role.status != "1":
                    continue
                scope = await resolve_data_scope_for_roles(
                    db, user=actor, roles=(role,), tenant=tenant
                )
                if scope.unbounded or (
                    dept_id is not None
                    and scope.accessible_dept_ids is not None
                    and int(dept_id) in scope.accessible_dept_ids
                ):
                    roles.append(str(role.role_id))
            for operation, fields in requirements.items():
                decision = resolve_record_fields(
                    catalog=USER_FIELDS,
                    trusted_grants=context.grants,
                    applicable_role_ids=frozenset(roles),
                    operation=operation,
                    purpose="management.relations"
                    if operation == "replace"
                    else "management.create",
                    entrypoint="ai-tool",
                )
                validate_write_fields(
                    supplied_paths=fields, decisions=decision, operation=operation
                )
            identity = resolve_record_fields(
                catalog=USER_FIELDS,
                trusted_grants=context.grants,
                applicable_role_ids=frozenset(roles),
                operation="read",
                purpose="management.read",
                entrypoint="ai-tool",
            )
            if identity.fields["user_name"].read != "plain":
                raise _denied("FIELD_CONFIRMATION_FACTS_REQUIRED")
            return context
        if args.get("user_id") is not None:
            ids = [int(args["user_id"])]
        elif tool == "user.batch_delete":
            predicates = []
            for key, column in (
                ("user_ids", User.user_id),
                ("user_names", User.user_name),
                ("phones", User.user_phone),
            ):
                values = args.get(key) or []
                if type(values) is not list or len(values) > 100:
                    raise _denied()
                if values:
                    predicates.append(column.in_(values))
            if predicates:
                ids = list(
                    await db.scalars(
                        select(User.user_id)
                        .where(
                            User.tenant_id == tenant.tenant_id,
                            *context.base_predicates,
                            or_(*predicates),
                        )
                        .order_by(User.user_id)
                        .limit(101)
                    )
                )
        if len(ids) > 100:
            raise _denied()
        read = await self.decisions(db, tx, context, ids)
        if set(read) != set(ids):
            raise _denied()
        necessary = {"user_name"} | set().union(*requirements.values()) - {"password"}
        for decision in read.values():
            if any(
                not decision.fields[name].allowed
                or decision.fields[name].read != "plain"
                for name in necessary
            ):
                raise _denied("FIELD_CONFIRMATION_FACTS_REQUIRED")
        for operation, fields in requirements.items():
            decisions = await self.decisions(
                db,
                tx,
                context,
                ids,
                operation,
                "management.relations"
                if operation == "replace"
                else "management.update",
            )
            for decision in decisions.values():
                validate_write_fields(
                    supplied_paths=fields, decisions=decision, operation=operation
                )
        return context

    async def project(self, db, tx, context, *, tool, result):
        if context is None or not result.ok or tool not in {"user.list", "user.lookup"}:
            return result
        data = dict(result.data)
        records = data["records"] if tool == "user.list" else [data]
        decisions = await self.decisions(
            db, tx, context, [int(row["id"]) for row in records]
        )
        if len(decisions) != len({row["id"] for row in records}):
            raise _denied("FIELD_AUTHORIZATION_CHANGED")
        ui = dict(result.ui.view_data)
        projected = []
        for row in records:
            decision = decisions[int(row["id"])]
            fields = {key: value for key, value in row.items() if key in USER_FIELDS}
            values = dict(
                project_user_values(
                    raw_values=fields,
                    decisions=decision,
                    entrypoint_caps=dict.fromkeys(fields, "plain"),
                ).values
            )
            output = {"id": row["id"], **values}
            if tool == "user.lookup":
                for root, keys in (
                    ("roles", ("roleAssignments", "roleAssignmentsComplete")),
                    (
                        "departments",
                        ("departmentAssignments", "departmentAssignmentsComplete"),
                    ),
                ):
                    if decision.fields[root].read == "plain":
                        output.update({key: row[key] for key in keys if key in row})
                visible = []
                for item in ui.get("fields", []):
                    name = _LABELS.get(item["label"].rsplit(".", 1)[-1])
                    if name is not None and decision.fields[name].allowed:
                        value = project_user_values(
                            raw_values={name: item["value"]},
                            decisions=decision,
                            entrypoint_caps={
                                name: "masked" if USER_FIELDS[name].mask_id else "plain"
                            },
                        ).values
                        if name in value:
                            visible.append(dict(item, value=value[name]))
                ui["fields"] = visible
                ui["title"] = (
                    values.get("nickname") or values.get("user_name") or "用户"
                )
            projected.append(output)
        if tool == "user.list":
            data.update(records=projected, sample=projected[:3])
            ui["rows"] = [
                {key: old[key] for key in row if key != "id"}
                for old, row in zip(ui["rows"], projected, strict=True)
            ]
            visible_keys = (
                set().union(*(set(row) for row in projected)) if projected else set()
            )
            ui["columns"] = [
                column for column in ui["columns"] if column["key"] in visible_keys
            ]
        else:
            data = projected[0]
        # These are copies of entity data too; regenerate from the projected DTO.
        audit = (
            {"total": data["total"]} if tool == "user.list" else {"user_id": data["id"]}
        )
        labels = (
            result.ui.label_params
            if tool == "user.list"
            else {"userName": data.get("user_name", "用户")}
        )
        return replace(
            result,
            data=data,
            ui=replace(result.ui, view_data=ui, audit=audit, label_params=labels),
        )


user_ai_fields = UserAIFields()
