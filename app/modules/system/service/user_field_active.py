"""Verified immutable policy snapshots under the caller's existing G/B fence.

None means never-enforced compatibility. An empty tuple means deny by default.
No requester can supply grants, mode, snapshot, or compatibility flags.
"""

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.system.field_policy.documents import (
    catalog_digest,
    document_digest,
    read_rules,
    rules_digest,
)
from app.modules.system.field_policy.state import valid_policy_state
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyBinding as Binding,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyRevision as Revision,
)
from app.modules.system.models.user_field_policy import UserFieldPolicyState as State
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.generic_field_policy_admission import (
    require_user_policy_compatibility,
)


def unavailable():
    return BusinessException(
        503, "用户字段策略不可用", error_code="FIELD_POLICY_UNAVAILABLE"
    )


async def active_policy_grants(db, tx, *, tenant_id, role_ids=()):
    tx.assert_session(db)
    tx.assert_barrier(tenant_id)
    await require_user_policy_compatibility(db, tenant_id=tenant_id)
    if (
        type(role_ids) is not tuple
        or len(role_ids) > 1024
        or any(type(r) is not str for r in role_ids)
    ):
        raise unavailable()
    with bounded_authorization_reads(
        db, AuthorizationReadBudget(max_rows=100000, max_bytes=128 * 1024 * 1024)
    ):
        state = (
            (
                await db.execute(
                    select(State.__table__).where(State.tenant_id == tenant_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            state is None
            or not valid_policy_state(
                state["mode"],
                state["ever_enforced"],
                state["candidate_manifest_digest"],
                state["active_manifest_digest"],
            )
            or state["catalog_digest"] != catalog_digest()
            or state["catalog_revision"] != CATALOG_REVISION
        ):
            raise unavailable()
        if state["mode"] in {"legacy", "ready"}:
            return None
        if state["mode"] != "enforcing" or not settings.USER_FIELD_POLICY_AVAILABLE:
            raise unavailable()
        items, grants, last = [], [], 0
        b, r = Binding.__table__, Revision.__table__
        while True:
            rows = (
                await db.execute(
                    select(
                        b.c.role_id,
                        b.c.revision_id,
                        b.c.version,
                        b.c.archived_at,
                        r.c.rules,
                        r.c.rules_digest,
                        r.c.catalog_digest,
                        r.c.catalog_revision,
                    )
                    .outerjoin(
                        r,
                        (r.c.tenant_id == b.c.tenant_id)
                        & (r.c.role_id == b.c.role_id)
                        & (r.c.revision_id == b.c.revision_id),
                    )
                    .where(b.c.tenant_id == tenant_id, b.c.role_id > last)
                    .order_by(b.c.role_id)
                    .limit(16)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                if (
                    len(items) >= 1024
                    or row.catalog_digest != catalog_digest()
                    or row.catalog_revision != CATALOG_REVISION
                ):
                    raise unavailable()
                try:
                    parsed = read_rules(row.rules, role_id=row.role_id)
                    if rules_digest(row.rules) != row.rules_digest:
                        raise unavailable()
                except BusinessException:
                    raise unavailable() from None
                items.append(
                    {
                        "role_id": str(row.role_id),
                        "revision_id": str(row.revision_id),
                        "version": row.version,
                        "archived": row.archived_at is not None,
                        "rules_digest": row.rules_digest,
                    }
                )
                if str(row.role_id) in role_ids and row.archived_at is None:
                    grants.extend(parsed)
            last = rows[-1].role_id
        if (
            not items
            or document_digest({"catalog_digest": catalog_digest(), "bindings": items})
            != state["active_manifest_digest"]
        ):
            raise unavailable()
        return tuple(grants)
