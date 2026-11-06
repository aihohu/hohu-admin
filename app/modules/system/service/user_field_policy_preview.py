"""Bounded value-free effective samples, authorized by the policy control plane."""

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.rbac import is_super_admin
from app.db.base import user_roles
from app.modules.auth.permission_collect import collect_user_buttons
from app.modules.system.field_policy.documents import catalog_digest, document_digest
from app.modules.system.field_policy.evaluator import FieldDecision, project_user_values
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from app.modules.system.service.user_field_active import (
    active_policy_grants,
    unavailable,
)
from app.modules.system.service.user_field_context import user_field_context
from app.modules.system.service.user_read_service import _legacy_rules

_OPERATIONS = (
    ("read", "management.read"),
    ("update", "management.update"),
    ("replace", "management.relations"),
    ("contains", "management.query"),
    ("export", "export"),
)


def _effective(decisions, actor):
    fields = dict(decisions.fields)
    if decisions.operation in {"read", "export"}:
        fields = dict(
            project_user_values(
                raw_values={},
                decisions=decisions,
                entrypoint_caps={
                    name: "plain"
                    for name, field in USER_FIELDS.items()
                    if field.readable
                },
            ).field_access
        )
    permissions = set(collect_user_buttons(actor))
    for name in fields:
        required = {
            "read": {"system:user:list"},
            "contains": {"system:user:list"},
            "update": {"system:user:edit"},
            "export": {"system:user:export"},
            "replace": {
                "system:user:edit",
                "system:user:role-auth" if name == "roles" else "system:dept:list",
            },
        }[decisions.operation]
        if not is_super_admin(actor) and not required <= permissions:
            fields[name] = FieldDecision()
    return fields


async def effective_preview(db, tx, *, tenant, role_id, grants, state):
    tx.assert_planning(tenant.tenant_id)
    bindings = await policies._bindings(db, tenant)
    candidates, items = [], []
    for binding in bindings:
        snapshot = await policies._snapshot(db, tenant, binding)
        items.append(
            {
                "role_id": str(binding["role_id"]),
                "revision_id": str(binding["revision_id"]),
                "version": binding["version"],
                "archived": binding["archived_at"] is not None,
                "rules_digest": snapshot.rules_digest,
            }
        )
        if not snapshot.archived and int(snapshot.role_id) != role_id:
            candidates.extend(snapshot.rules)
    digest = (
        document_digest({"catalog_digest": catalog_digest(), "bindings": items})
        if items
        else None
    )
    if digest != state["candidate_manifest_digest"]:
        raise unavailable()
    candidates.extend(grants)
    subjects = list(
        (
            await db.scalars(
                select(User)
                .join(
                    user_roles,
                    (user_roles.c.tenant_id == User.tenant_id)
                    & (user_roles.c.user_id == User.user_id),
                )
                .where(
                    User.tenant_id == tenant.tenant_id,
                    user_roles.c.role_id == role_id,
                    User.status == "1",
                )
                .options(
                    selectinload(User.roles).selectinload(Role.menus),
                    selectinload(User.depts),
                )
                .order_by(User.user_id)
                .limit(6)
            )
        ).all()
    )
    samples, truncated = [], len(subjects) > 5
    for actor in subjects[:5]:
        role_ids = tuple(str(r.role_id) for r in actor.roles if r.status == "1")
        # Frozen has no deliverable 'before'; proposed repair remains previewable.
        active = (
            ()
            if state["mode"] == "frozen"
            else await active_policy_grants(
                db, tx, tenant_id=tenant.tenant_id, role_ids=role_ids
            )
        )
        before_grants = _legacy_rules(role_ids) if active is None else active
        after_grants = tuple(rule for rule in candidates if rule.role_id in role_ids)
        before = await user_field_context.for_actor(
            db, tx, tenant=tenant, actor=actor, grants=before_grants
        )
        after = replace(before, grants=after_grants, _applicable={})
        ids = tuple(
            (
                await db.scalars(
                    select(User.user_id)
                    .where(User.tenant_id == tenant.tenant_id, *before.base_predicates)
                    .order_by(User.user_id)
                    .limit(21)
                )
            ).all()
        )
        truncated = truncated or len(ids) > 20
        ids = ids[:20]
        for operation, purpose in _OPERATIONS:
            entrypoint = "export" if operation == "export" else "user-list"
            old = await before.decisions(
                db,
                tx,
                user_ids=ids,
                operation=operation,
                purpose=purpose,
                entrypoint=entrypoint,
            )
            new = await after.decisions(
                db,
                tx,
                user_ids=ids,
                operation=operation,
                purpose=purpose,
                entrypoint=entrypoint,
            )
            for uid in ids:
                old_fields, new_fields = (
                    _effective(old[uid], actor),
                    _effective(new[uid], actor),
                )
                for field, decision in new_fields.items():
                    previous = old_fields[field]
                    if previous != decision:
                        samples.append(
                            {
                                "subjectUserId": str(actor.user_id),
                                "recordUserId": str(uid),
                                "fieldId": field,
                                "operation": operation,
                                "purpose": purpose,
                                "before": {
                                    "allowed": previous.allowed,
                                    "read": previous.read,
                                },
                                "after": {
                                    "allowed": decision.allowed,
                                    "read": decision.read,
                                },
                            }
                        )
    return {
        "effectiveSamples": samples,
        "samplesTruncated": truncated,
        "queryCoverageVerified": False,
    }
