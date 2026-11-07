"""Management projections and preview preparation under the caller's G/B proof."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import canonical, primitive
from app.core.field_policy.documents import (
    catalog_parts,
    digest,
    load_rules,
    rules_document,
)
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.field_policy.management_preview import (
    issue_preview,
    verify_preview,
)
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.schemas.field_management import parse_management
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)


def forbidden():
    return BusinessException(
        403, "字段策略管理权限不足", error_code="FIELD_POLICY_MANAGEMENT_FORBIDDEN"
    )


def wire(value, name=""):
    """Do not transform field identifiers/enum values, only DTO member names."""
    from pydantic.alias_generators import to_camel  # noqa: PLC0415

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {to_camel(k): wire(v, k) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    if type(value) is int and (
        name.endswith(("_id", "Id", "_version", "Version", "_epoch", "Epoch"))
        or name == "epoch"
    ):
        return str(value)
    return value


def field_metadata(field):
    result = primitive(field)
    result["bulk_eligible"] = not field.structural and field.field_id not in {
        "password",
        "hashed_password",
        "auth_version",
        "tenant_id",
    }
    result["choices"] = []
    for access in field.accesses:
        # These recommendations are deliberately conservative; sensitive query is opt-in.
        safe = not field.structural and (
            access.operation == "read"
            or (
                access.operation in {"exact", "eq"}
                and access.purpose == "management.query"
                and access.use == "filter"
                and field.mask_id is None
                and not field.sources
            )
        )
        levels = ["unset", "deny"]
        if not field.structural:
            if access.operation in {"read", "export"}:
                if field.mask_id and field.read_cap in {"masked", "plain"}:
                    levels.append("masked")
                if field.read_cap == "plain":
                    levels.append("plain")
            else:
                levels.append("allow")
        result["choices"].append(
            {"access": primitive(access), "values": levels, "safe_recommendation": safe}
        )
    return result


async def snapshot(db, tx, tenant, catalog):
    tx.assert_barrier(tenant.tenant_id)
    await policies._catalog(db, catalog)
    head = dict(await policies._head(db, tenant.tenant_id))
    objects = [
        dict(row) for row in await policies._objects(db, tenant.tenant_id, catalog)
    ]
    bindings = await policies._bindings(db, tenant.tenant_id, catalog)
    g_epoch = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    b_epoch = await db.scalar(
        select(UserFieldPolicyState.epoch).where(
            UserFieldPolicyState.tenant_id == tenant.tenant_id
        )
    )
    return head, objects, bindings, g_epoch, b_epoch


async def managed_role(db, tenant, role_id):
    if type(role_id) is not int or not 0 < role_id < 2**63:
        raise forbidden()
    row = (
        (
            await db.execute(
                select(
                    Role.role_id,
                    Role.role_name,
                    Role.role_code,
                    Role.data_scope,
                    Role.status,
                ).where(Role.tenant_id == tenant.tenant_id, Role.role_id == role_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise forbidden()
    return dict(row)


def canonical_draft(catalog, tenant, role_id, object_id, payload):
    # Editing order is not persistence order. Duplicate keys still fail load_rules.
    ordered = sorted(
        payload["rules"],
        key=lambda rule: (
            rule["field_id"],
            rule["access"]["purpose"],
            rule["access"]["operation"],
            rule["access"]["use"],
        ),
    )
    grants = load_rules(
        catalog,
        ordered,
        tenant_id=tenant.tenant_id,
        role_id=role_id,
        object_id=object_id,
    )
    return rules_document(
        catalog,
        grants,
        tenant_id=tenant.tenant_id,
        role_id=role_id,
        object_id=object_id,
    )


def mutation_command(operation, payload, role_id, object_id):
    command = {
        "action": operation,
        "request_id": payload["request_id"],
        "reason": payload["reason"],
        "expected_version": int(payload["expected_version"]),
    }
    if operation == "publish":
        command.update(
            role_id=role_id,
            object_id=object_id,
            binding_version=int(payload["expected_binding_version"]),
            rules=payload["rules"],
        )
    if operation == "freeze":
        command.update(
            object_id=payload["object_id"],
            expected_epoch=int(payload["expected_epoch"]),
        )
    return command


def differences(before, after):
    def keyed(rules):
        return {
            (
                r["field_id"],
                *(r["access"][k] for k in ("operation", "purpose", "use")),
            ): r
            for r in rules
        }

    old, new = keyed(before), keyed(after)
    result = [
        {
            "field_id": k[0],
            "access": (new.get(k) or old[k])["access"],
            "before": old.get(k),
            "after": new.get(k),
        }
        for k in sorted(old.keys() | new.keys())
        if old.get(k) != new.get(k)
    ]
    return result[:20], len(result) > 20, len(result)


async def prepare_management(
    db, tx, *, tenant, actor, catalog, operation, payload, role_id=None, object_id=None
):
    """Return a detached DTO or a command for the existing complete lock planner."""
    payload = parse_management(operation, payload)
    canonical(payload, 524288)
    if operation == "receipt":
        return wire(
            await policies.receipt(db, tenant=tenant, request_id=payload["request_id"])
        ), None
    role = None
    if operation in {"object", "preview", "publish"}:
        role = await managed_role(db, tenant, role_id)
        if (
            object_id not in catalog.objects
            or catalog.objects[object_id].domain_kind != "tenant"
        ):
            raise BusinessException(
                422, "业务对象不可配置", error_code="FIELD_POLICY_INVALID"
            )
        if "rules" in payload:
            payload["rules"] = canonical_draft(
                catalog, tenant, role_id, object_id, payload
            )
    command = None
    if operation in {"publish", "build", "freeze"}:
        command = mutation_command(operation, payload, role_id, object_id)
        # A receipt is a fact, never permission to replay a write. Current authority was checked by owner.
        prior = await policies.receipt(
            db,
            tenant=tenant,
            request_id=payload["request_id"],
            command_digest=digest(command),
        )
        if prior is not None:
            return wire(prior), None
    head, objects, bindings, g_epoch, b_epoch = await snapshot(db, tx, tenant, catalog)
    codes = collect_user_permission_codes(actor)
    capabilities = {
        "view": "system:role:field-policy-view" in codes,
        "edit": "system:role:field-policy-edit" in codes,
        "activate": False,
        "freeze": "system:role:field-policy-activate" in codes,
    }
    common = {
        "catalog_digest": catalog.digest,
        "head_version": head["head_version"],
        "epoch": b_epoch,
        "active_manifest_digest": head["active_manifest_digest"],
        "candidate_manifest_digest": head["candidate_manifest_digest"],
        "tenant_id": tenant.tenant_id,
        "capabilities": capabilities,
    }
    if operation == "catalog":
        start, size = int(payload["cursor"]), payload["limit"]
        values = [
            primitive(obj) | {"fields": [field_metadata(f) for f in obj.fields]}
            for obj in catalog.objects.values()
            if obj.domain_kind == "tenant"
        ]
        page = values[start : start + size]
        return wire(
            common
            | {
                "objects": page,
                "next_cursor": str(start + size)
                if start + size < len(values)
                else None,
                "action_catalog_digest": catalog_parts(catalog)[0],
                "complete": start + size >= len(values),
            }
        ), None
    if operation == "roles":
        rows = (
            (
                await db.execute(
                    select(
                        Role.role_id,
                        Role.role_name,
                        Role.role_code,
                        Role.data_scope,
                        Role.status,
                    )
                    .where(
                        Role.tenant_id == tenant.tenant_id,
                        Role.role_id > int(payload["cursor"]),
                    )
                    .order_by(Role.role_id)
                    .limit(payload["limit"] + 1)
                )
            )
            .mappings()
            .all()
        )
        more = len(rows) > payload["limit"]
        return wire(
            common
            | {
                "records": [dict(r) for r in rows[: payload["limit"]]],
                "next_cursor": str(rows[-2]["role_id"]) if more else None,
            }
        ), None
    binding = next(
        (b for b in bindings if (b["role_id"], b["object_id"]) == (role_id, object_id)),
        None,
    )
    rules = binding["rules"] if binding and binding["archived_at"] is None else []
    if operation == "object":
        return wire(
            common
            | {
                "role": role,
                "object_id": object_id,
                "binding_version": binding["binding_version"] if binding else 0,
                "revision_id": binding["revision_id"] if binding else None,
                "rules": rules,
            }
        ), None
    items = policies._item_rows(bindings)
    if operation == "manifest":
        return wire(
            common
            | {
                "objects": objects,
                "items": items,
                "can_activate": False,
                "activation_blockers": ["FULL_SYSTEM_ACCEPTANCE_REQUIRED"],
                "rollout_state": head["rollout_state"],
            }
        ), None
    if operation == "freeze":
        return None, command
    if payload["catalog_digest"] != catalog.digest:
        raise BusinessException(
            409, "目录已变化，请重载", error_code="FIELD_POLICY_CATALOG_CONFLICT"
        )
    if int(payload["expected_version"]) != head["head_version"] or (
        "expected_binding_version" in payload
        and int(payload["expected_binding_version"])
        != (binding["binding_version"] if binding else 0)
    ):
        raise BusinessException(
            409, "候选版本已变化，请重载", error_code="FIELD_POLICY_VERSION_CONFLICT"
        )
    context = {
        "tenant": tenant.tenant_id,
        "actor": tenant.actor_user_id,
        "user_version": actor.auth_version,
        "tenant_version": tenant.tenant_version,
        "g": g_epoch,
        "b": b_epoch,
        "head": head["head_version"],
        "active": head["active_manifest_digest"],
        "catalog": catalog.digest,
        "actions": catalog_parts(catalog)[0],
        "role": role_id if role else None,
        "object": object_id if role else None,
        "binding": binding["binding_version"] if binding else 0,
        "target": payload.get("rules", items),
    }
    if operation in {"publish", "build"}:
        verify_preview(payload["preview_digest"], context)
        return None, command
    token, expiry = issue_preview(context)
    diff, truncated, count = (
        differences(rules, payload["rules"])
        if operation == "preview"
        else ([], False, len(items))
    )
    return wire(
        common
        | {
            "preview_digest": token,
            "expires_at": datetime.fromtimestamp(expiry, UTC),
            "diff": diff,
            "change_count": count,
            "sample_truncated": truncated,
            "safe_samples": [],
            "items": items if operation == "manifest-preview" else [],
            "dependency_changes": [],
        }
    ), None
