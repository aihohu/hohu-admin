"""Tenant control plane; never a source of business field grants."""

from sqlalchemy import select

from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    NotFoundException,
)
from app.core.rbac import is_super_admin
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.field_policy.documents import canonical_rules, catalog_digest
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS
from app.modules.system.models.role import Role
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from app.modules.system.service.user_field_context import load_current_user

PERMISSIONS = {
    "read": "system:user:field-policy-view",
    "preview": "system:user:field-policy-view",
    "publish": "system:user:field-policy-edit",
    "ready": "system:user:field-policy-edit",
    "activate": "system:user:field-policy-activate",
    "freeze": "system:user:field-policy-activate",
}

PURPOSE_OPERATIONS = {
    "management.read": ("read",),
    "management.create": ("create",),
    "management.update": ("update",),
    "management.relations": ("replace",),
    "management.query": ("exact", "contains", "eq", "group", "distinct"),
    "export": ("export",),
    "import": ("create", "update", "replace", "exact"),
    "own-account": ("read", "update"),
}


def policy_catalog():
    fields = []
    for name, field in USER_FIELDS.items():
        options = []
        for purpose, operations in PURPOSE_OPERATIONS.items():
            for operation in operations:
                try:
                    canonical_rules(
                        (FieldGrant("1", name, operation, purpose),), role_id=1
                    )
                except BusinessException:
                    continue
                options.append({"purpose": purpose, "operation": operation})
        if options:
            fields.append(
                {
                    "fieldId": name,
                    "readable": field.readable,
                    "maskId": field.mask_id,
                    "options": options,
                }
            )
    return {
        "catalogRevision": CATALOG_REVISION,
        "catalogDigest": catalog_digest(),
        "fields": fields,
    }


def policy_rule_diff(before, after):
    def index(rules):
        return {
            (r.field_id, r.operation, r.purpose): {
                "effect": r.effect,
                "read": r.read,
                "maskId": r.mask_id,
            }
            for r in rules
        }

    old, new = index(before), index(after)
    return [
        {
            "fieldId": key[0],
            "operation": key[1],
            "purpose": key[2],
            "before": old.get(key),
            "after": new.get(key),
        }
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]


def policy_wire(snapshot):
    if snapshot is None:
        return None
    return {
        "roleId": snapshot.role_id,
        "revisionId": snapshot.revision_id,
        "version": snapshot.version,
        "archived": snapshot.archived,
        "rulesDigest": snapshot.rules_digest,
        "createdAt": snapshot.created_at.isoformat(),
        "rules": [
            {
                "fieldId": rule.field_id,
                "operation": rule.operation,
                "purpose": rule.purpose,
                "effect": rule.effect,
                "read": rule.read,
                "maskId": rule.mask_id,
            }
            for rule in snapshot.rules
        ],
    }


def state_wire(state, blockers=("FIELD_POLICY_QUALIFICATION_REQUIRED",)):
    blockers = list(blockers)
    if state["mode"] not in {"ready", "frozen"}:
        blockers.append("FIELD_POLICY_READY_REQUIRED")
    return {
        "mode": state["mode"],
        "epoch": state["epoch"],
        "catalogDigest": state["catalog_digest"],
        "candidateManifestDigest": state["candidate_manifest_digest"],
        "activeManifestDigest": state["active_manifest_digest"],
        "everEnforced": state["ever_enforced"],
        "canActivate": not blockers,
        "activationBlockers": blockers,
    }


class UserFieldPolicyAdmin:
    async def authorize(self, db, tx, *, tenant, actor_user_version, operation):
        tx.assert_planning(tenant.tenant_id)
        actor = await load_current_user(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        required = PERMISSIONS.get(operation)
        if (
            required is None
            or required not in collect_user_permission_codes(actor)
            or not is_super_admin(actor)
        ):
            raise AuthorizationException(
                "字段策略管理权限不足", error_code="FIELD_POLICY_MANAGEMENT_FORBIDDEN"
            )

    async def read(self, db, tx, *, tenant, role_id):
        tx.assert_planning(tenant.tenant_id)
        if (
            await db.scalar(
                select(Role.role_id).where(
                    Role.tenant_id == tenant.tenant_id, Role.role_id == role_id
                )
            )
            is None
        ):
            raise NotFoundException("角色")
        state = await policies._state(db, tenant)
        if state["catalog_digest"] != catalog_digest():
            raise BusinessException(
                503, "字段目录不可用", error_code="FIELD_POLICY_UNAVAILABLE"
            )
        binding = await policies._binding(db, tenant, role_id)
        snapshot = (
            None if binding is None else await policies._snapshot(db, tenant, binding)
        )
        return state, snapshot

    @staticmethod
    def validate_draft(draft, snapshot, role_id):
        if draft.catalog_digest != catalog_digest():
            raise BusinessException(
                409, "字段目录已变更", error_code="FIELD_POLICY_CATALOG_CONFLICT"
            )
        if draft.expected_version != (snapshot.version if snapshot else 0):
            raise BusinessException(
                409,
                "策略已更新，请重新加载",
                error_code="FIELD_POLICY_VERSION_CONFLICT",
            )
        return draft.grants(role_id)


user_field_policy_admin = UserFieldPolicyAdmin()
