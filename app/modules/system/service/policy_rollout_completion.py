"""Read-only complete rollout proof, held under the single global lock protocol."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_parts, digest
from app.core.field_policy.qualification import Qualification
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)
from app.modules.system.service.generic_field_policy_state import read_manifest


def blocked():
    return BusinessException(
        409,
        "全租户字段状态尚未完成核验",
        error_code="FIELD_POLICY_ACTIVATION_BLOCKED",
    )


async def capture_completion(db, tx, *, catalog, qualification):
    tx.assert_session(db)
    tenants = tx.assert_global_barriers()
    if type(qualification) is not Qualification:
        raise blocked()
    qualification.verify(catalog, settings.RELEASE_BUILD_SHA)
    await policies._catalog(db, catalog)
    registry = (
        (
            await db.execute(
                select(
                    Tenant.tenant_id,
                    Tenant.status,
                    Tenant.lifecycle_state,
                    Tenant.row_version,
                    Tenant.bootstrap_version,
                )
                .order_by(Tenant.tenant_id)
                .limit(1025)
            )
        )
        .mappings()
        .all()
    )
    heads = (
        (
            await db.execute(
                select(FieldPolicyHead.__table__)
                .order_by(FieldPolicyHead.tenant_id)
                .limit(1025)
            )
        )
        .mappings()
        .all()
    )
    barriers = (
        (
            await db.execute(
                select(UserFieldPolicyState.__table__)
                .order_by(UserFieldPolicyState.tenant_id)
                .limit(1025)
            )
        )
        .mappings()
        .all()
    )
    if (
        not 1 <= len(tenants) <= 1024
        or tuple(row["tenant_id"] for row in registry) != tenants
        or tuple(h["tenant_id"] for h in heads) != tenants
        or tuple(b["tenant_id"] for b in barriers) != tenants
    ):
        raise blocked()
    global_epoch = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    snapshots, quarantined = [], 0
    for head, barrier, tenant in zip(heads, barriers, registry, strict=True):
        active = (tenant["status"], tenant["lifecycle_state"]) == ("1", "active")
        if not active and (
            tenant["status"] != "2"
            or tenant["lifecycle_state"] not in {"prepared", "disabled"}
        ):
            raise blocked()
        quarantined += not active
        if head["catalog_digest"] != catalog.digest or (
            active
            and (
                head["rollout_state"] != "enforcing"
                or not head["ever_enforced"]
                or head["active_manifest_id"] is None
            )
        ):
            raise blocked()
        tenant_id = head["tenant_id"]
        objects = await policies._objects(db, tenant_id, catalog)
        items = ()
        if head["active_manifest_id"] is not None:
            if not head["ever_enforced"] or head["rollout_state"] not in {
                "enforcing",
                "frozen",
            }:
                raise blocked()
            _, items, _ = await read_manifest(
                db,
                tenant_id=tenant_id,
                manifest_id=head["active_manifest_id"],
                expected_digest=head["active_manifest_digest"],
                catalog=catalog,
            )
        elif (
            active
            or head["ever_enforced"]
            or head["rollout_state"] != "preparing"
            or barrier["ever_enforced"]
            or barrier["mode"] not in {"legacy", "ready"}
        ):
            raise blocked()
        for obj in objects:
            if head["active_manifest_id"] is None:
                if (
                    obj["mode"] not in {"legacy", "ready"}
                    or obj["ever_enforced"]
                    or obj["active_object_digest"] is not None
                ):
                    raise blocked()
                continue
            expected = digest(
                {
                    "catalog": catalog.digest,
                    "object": obj["object_id"],
                    "items": [
                        item for item in items if item["object_id"] == obj["object_id"]
                    ],
                },
                4 * 1024 * 1024,
            )
            if (
                obj["mode"]
                not in ({"enforcing"} if active else {"enforcing", "frozen"})
                or not obj["ever_enforced"]
                or obj["active_object_digest"] != expected
            ):
                raise blocked()
            if obj["object_id"] == "system.user" and (
                barrier["mode"] != obj["mode"]
                or not barrier["ever_enforced"]
                or barrier["active_manifest_digest"] != expected
            ):
                raise blocked()
        snapshots.append(
            {
                "tenant": str(tenant_id),
                "status": tenant["status"],
                "lifecycle": tenant["lifecycle_state"],
                "row_version": str(tenant["row_version"]),
                "bootstrap_version": str(tenant["bootstrap_version"]),
                "epoch": str(barrier["epoch"]),
                "head_version": str(head["head_version"]),
                "manifest_id": str(head["active_manifest_id"])
                if head["active_manifest_id"] is not None
                else None,
                "manifest_digest": head["active_manifest_digest"],
                "objects": [
                    {
                        "id": obj["object_id"],
                        "version": str(obj["object_version"]),
                        "digest": obj["active_object_digest"],
                        "mode": obj["mode"],
                        "ever_enforced": obj["ever_enforced"],
                    }
                    for obj in objects
                ],
            }
        )
    qualification.verify(catalog, settings.RELEASE_BUILD_SHA)
    action, adapter = catalog_parts(catalog)
    return {
        "schema_version": 1,
        "build_sha": settings.RELEASE_BUILD_SHA,
        "catalog_digest": catalog.digest,
        "action_digest": action,
        "adapter_digest": adapter,
        "qualification_digest": qualification.evidence_digest,
        "expires_at": qualification.expires_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "tenant_count": len(tenants),
        "quarantined_count": quarantined,
        "state_digest": digest(
            {"global_epoch": str(global_epoch), "tenants": snapshots}, 4 * 1024 * 1024
        ),
    }
