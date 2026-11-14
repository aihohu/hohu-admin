"""Capture code-declared sources after SQL proofs; no business value is stored."""

from dataclasses import replace

from sqlalchemy import select

from app.core.field_policy.contracts import limit
from app.modules.ai.service.delivery_source_domains import RECORD_LIMITS, source_domain
from app.modules.ai.service.delivery_source_revalidation import MODELS
from app.modules.system.field_policy.ai_plan import ai_query_plan, ai_write_fields
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role


async def capture_domain(
    db, context, obj, *, readable=(), queries=(), records=None, levels=None
):
    model, anchor = MODELS[obj]
    table = model.__table__
    if records is None:
        maximum = RECORD_LIMITS.get(obj, 5000)
        records = tuple(
            await db.scalars(
                select(table.c[anchor])
                .where(context.base(obj, table))
                .order_by(table.c[anchor])
                .limit(maximum + 1)
            )
        )
        limit(len(records) <= maximum)
    reads = tuple(
        (name, (levels or {}).get(name, "plain")) for name in sorted(set(readable))
    )
    uses = tuple(
        (name, access.operation, access.purpose, access.use)
        for name, access in dict.fromkeys(queries)
    )
    return source_domain(
        action_id=context.action,
        object_id=obj,
        records=tuple(records),
        read=reads,
        query=uses,
    )


async def capture_user_query(db, context, tool, args):
    if tool not in {
        "user.list",
        "user.lookup",
        "user.count",
        "user.stats",
        "user.distinct",
        "user.update",
        "user.reset_password",
        "user.batch_delete",
        "user.update_roles",
        "user.update_dept",
        "user.create",
    }:
        return []
    plan = ai_query_plan(tool, args)
    from app.core.field_policy import Access  # noqa: PLC0415

    queries = tuple(
        (d.field_id, Access(d.operation, d.purpose, d.use))
        for d in plan.dependencies
        if d.use != "fixed_order"
    )
    return [await capture_domain(db, context, "system.user", queries=queries)]


async def capture_preflight_user(db, context, user_context, tool, args):
    if user_context is None or tool == "user.create":
        return []
    records = tuple(sorted(user_context._applicable))
    if not records:
        return []
    requirements = ai_write_fields(tool, args)
    fields = {"user_name"} | (
        set().union(*requirements.values()) if requirements else set()
    ) - {"password"}
    return [
        await capture_domain(
            db, context, "system.user", readable=fields, records=records
        )
    ]


async def project_lookup_relations(db, context, data):
    groups = []
    for key, complete, obj, model, anchor, fields in (
        (
            "roleAssignments",
            "roleAssignmentsComplete",
            "system.role",
            Role,
            "role_id",
            ("role_name", "role_code", "status", "data_scope"),
        ),
        (
            "departmentAssignments",
            "departmentAssignmentsComplete",
            "system.dept",
            Dept,
            "dept_id",
            ("dept_name", "parent_id", "ancestors", "status"),
        ),
    ):
        if key not in data:
            continue
        wire_id = "roleId" if obj == "system.role" else "deptId"
        identifiers = tuple(sorted({int(row[wire_id]) for row in data[key]}))
        rows = await context.rows(
            db,
            obj,
            model,
            anchor,
            names=set(fields),
            predicates=(model.__table__.c[anchor].in_(identifiers),),
            maximum=len(identifiers) + 1,
        )
        allowed = {
            int(record.record_key)
            for record, values, _ in rows
            if set(fields) <= values.keys()
        }
        if allowed != set(identifiers):
            data.pop(key, None)
            data[complete] = False
            continue
        groups.append(
            await capture_domain(db, context, obj, readable=fields, records=identifiers)
        )
    return groups


async def capture_user_result(db, context, user_context, *, tool, result, tx):
    if (
        user_context is None
        or not result.ok
        or tool not in {"user.list", "user.lookup"}
    ):
        return result, []
    data = dict(result.data)
    rows = data["records"] if tool == "user.list" else [data]
    ids = tuple(sorted({int(row["id"]) for row in rows}))
    decisions = await user_context.decisions(
        db,
        tx,
        user_ids=ids,
        operation="read",
        purpose="management.read",
        entrypoint="ai-tool",
        _after_plan=True,
    )
    groups = []
    for row in rows:
        identifier = int(row["id"])
        names = tuple(name for name in row if name in USER_FIELDS)
        if tool == "user.lookup":
            names += tuple(
                root
                for key, root in (
                    ("roleAssignments", "roles"),
                    ("departmentAssignments", "departments"),
                )
                if key in data
            )
        levels = {}
        for name in names:
            level = decisions[identifier].fields[name].read
            # Both the known UI formatter and the model serializer cap these
            # fields. This records the declared cap, not a string-based guess.
            levels[name] = "masked" if USER_FIELDS[name].mask_id else level
        if names:
            groups.append(
                await capture_domain(
                    db,
                    context,
                    "system.user",
                    readable=names,
                    records=(identifier,),
                    levels=levels,
                )
            )
        if tool == "user.lookup":
            groups.extend(await project_lookup_relations(db, context, data))
    if tool == "user.lookup":
        result = replace(result, data=data)
    return result, groups


async def capture_members(db, context, *, tool, snapshot):
    if not tool.startswith(("role.", "dept.")) or type(snapshot) is not dict:
        return []
    facts = snapshot.get("facts", snapshot)
    identifiers = tuple(int(v) for v in facts.get("userIds", ()))
    if not identifiers:
        return []
    return [
        await capture_domain(
            db,
            context,
            "system.user",
            records=identifiers,
            readable=("roles" if tool.startswith("role.") else "departments",),
        )
    ]
