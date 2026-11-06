"""Presence-based field authorization inside the System owner's complete plan."""

from dataclasses import replace

from app.core.authorization_plan import ResourceKind
from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.dto import resolve_own_account_fields
from app.modules.system.field_policy.evaluator import (
    resolve_record_fields,
    validate_write_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.field_policy.write_plan import (
    user_write_footprint,
    validate_legacy_user_payload,
)
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_context import (
    load_current_user,
    user_field_context,
)
from app.utils.data_scope import resolve_data_scope_for_roles


async def validate_system_fields(
    db, tx, *, tenant, actor_user_version, operation, payload, target_id, facts
):
    if not operation.startswith("user.") and operation != "dept.members":
        return
    # Password changes/revocation are minimal identity security operations, not
    # a route to read or update protected profile fields.
    if operation in {"user.change_password", "user.revoke"}:
        return
    actor = await load_current_user(
        db, tenant=tenant, actor_user_version=actor_user_version
    )
    roles = tuple(str(r.role_id) for r in actor.roles if r.status == "1")
    grants = await active_policy_grants(
        db, tx, tenant_id=tenant.tenant_id, role_ids=roles
    )
    if grants is None:
        if operation in {"user.create", "user.update", "user.profile"}:
            validate_legacy_user_payload(payload)
        return
    if operation in {"user.delete", "user.batch_delete"}:
        return  # Existing function/record/delete-reference authority still applies.
    context = await user_field_context.load(
        db, tx, tenant=tenant, actor_user_version=actor_user_version, grants=grants
    )
    if operation in {"user.create", "user.update", "user.profile"}:
        groups = user_write_footprint(payload)
    elif operation in {"user.roles", "user.departments", "dept.members"}:
        groups = {
            ("replace", "management.relations"): (
                "roles" if operation == "user.roles" else "departments",
            )
        }
    elif operation == "user.password":
        groups = {("update", "management.update"): ("password",)}
    else:
        raise AuthorizationException(
            "字段写入口未登记", error_code="FIELD_WRITE_DENIED"
        )
    applicable = set()
    if operation == "user.create":
        proposed_depts = {int(item.dept_id) for item in payload.dept_ids}
        for role in actor.roles:
            if role.status != "1":
                continue
            scope = await resolve_data_scope_for_roles(
                db, user=actor, roles=(role,), tenant=tenant
            )
            if scope.unbounded or (
                proposed_depts
                and scope.accessible_dept_ids is not None
                and proposed_depts <= set(scope.accessible_dept_ids)
            ):
                applicable.add(str(role.role_id))
    if operation == "dept.members":
        # Validate the entire old and proposed set, including unchanged entries.
        ids = {
            row["user_id"]
            for row in facts[ResourceKind.USER_DEPTS]
            if row["dept_id"] == target_id
        }
        ids.update(int(uid) for uid in payload)
    else:
        ids = {target_id}
    for (action, purpose), fields in groups.items():
        if operation == "user.profile":
            decision = resolve_own_account_fields(
                actor=(tenant.tenant_id, tenant.actor_user_id),
                target=(tenant.tenant_id, target_id),
                grants=grants,
                applicable_role_ids=frozenset(roles),
                operation="update",
            )
            validate_write_fields(
                supplied_paths=fields, decisions=decision, operation=action
            )
        elif operation == "user.create":
            decision = resolve_record_fields(
                catalog=USER_FIELDS,
                trusted_grants=grants,
                applicable_role_ids=frozenset(applicable),
                operation=action,
                purpose=purpose,
                entrypoint="user-detail",
            )
            validate_write_fields(
                supplied_paths=fields, decisions=decision, operation=action
            )
        else:
            ordered = tuple(sorted(ids))
            for offset in range(0, len(ordered), 100):
                window = ordered[offset : offset + 100]
                decisions = await replace(context, _applicable={}).decisions(
                    db,
                    tx,
                    user_ids=window,
                    operation=action,
                    purpose=purpose,
                    entrypoint="user-detail",
                )
                if set(decisions) != set(window):
                    raise AuthorizationException(
                        "字段写入范围不足", error_code="FIELD_WRITE_DENIED"
                    )
                for decision in decisions.values():
                    validate_write_fields(
                        supplied_paths=fields, decisions=decision, operation=action
                    )
