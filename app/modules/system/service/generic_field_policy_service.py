"""Planned generic policy persistence. Current management authority belongs to owner."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, tuple_

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import canonical, identity, primitive, require
from app.core.field_policy.documents import (
    catalog_parts,
    digest,
    load_catalog,
    load_rules,
)
from app.core.id_generator import next_id
from app.core.tenant_scope import require_tenant_context
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.modules.system.models.field_policy import (
    FieldCatalogObject as CatalogObject,
)
from app.modules.system.models.field_policy import (
    FieldCatalogSnapshot as CatalogRow,
)
from app.modules.system.models.field_policy import (
    FieldPolicyBinding as Binding,
)
from app.modules.system.models.field_policy import (
    FieldPolicyHead as Head,
)
from app.modules.system.models.field_policy import (
    FieldPolicyObjectState as ObjectState,
)
from app.modules.system.models.field_policy import (
    FieldPolicyRequest as Request,
)
from app.modules.system.models.field_policy import (
    FieldPolicyRevision as Revision,
)
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyState as Barrier
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.generic_field_policy_state import (
    read_manifest,
    transition,
)

READY_FIELDS = {
    "ready_manifest_digest": None,
    "qualification_digest": None,
    "ready_g_epoch": None,
    "ready_b_epoch": None,
    "ready_until": None,
}


def failure(code="FIELD_POLICY_UNAVAILABLE", status=503):
    return BusinessException(status, "字段策略持久化状态不可用", error_code=code)


def key(kind, tenant_id, value):
    return ResourceKey(kind, tenant_id, value)


def request_identity(value):
    require(type(value) is str and len(value) == 36)
    try:
        require(str(UUID(value)) == value)
    except ValueError:
        raise failure("FIELD_POLICY_INVALID", 400) from None


def command_bytes(command):
    require(type(command) is dict)
    action = command.get("action")
    extra = {
        "publish": {"role_id", "object_id", "binding_version", "rules"},
        "archive": {"role_id", "object_id", "binding_version"},
        "build": set(),
        "ready": {"expected_epoch"},
        "activate": {"expected_epoch"},
        "freeze": {"expected_epoch", "object_id"},
    }
    require(type(action) is str and action in extra)
    require(
        set(command)
        == {"action", "request_id", "expected_version", "reason"} | extra[action]
    )
    for name in ("expected_version", "expected_epoch", "binding_version"):
        if name in command:
            require(
                type(command[name]) is int
                and (0 if name == "binding_version" else 1) <= command[name] < 2**63 - 1
            )
    require(
        type(command["reason"]) is str
        and 1 <= len(command["reason"].strip()) <= 500
        and command["reason"].isprintable()
    )
    request_identity(command["request_id"])
    if "role_id" in command:
        require(type(command["role_id"]) is int)
        identity(str(command["role_id"]))
    return canonical(command, 512 * 1024)


def manifest_document(catalog, tenant_id, base, objects, items):
    action, adapter = catalog_parts(catalog)
    return {
        "schema_version": 2,
        "tenant_id": str(tenant_id),
        "base_active_digest": base,
        "catalog_digest": catalog.digest,
        "action_catalog_digest": action,
        "adapter_digest": adapter,
        "objects": sorted(objects),
        "items": sorted(items, key=lambda row: (int(row["role_id"]), row["object_id"])),
    }


@dataclass(frozen=True, slots=True)
class GenericPolicyPlan:
    transaction: object
    tenant_id: int
    actor_id: int
    catalog_digest: str
    command: bytes
    lock_plan: LockPlan
    revision_id: int
    manifest_id: int
    audit_id: int
    qualification: object = None


class GenericFieldPolicyService:
    async def load_active(self, db, tx, *, tenant_id, catalog, object_id, role_ids):
        """Explicit caller catalog and role set; unknown/preparing/frozen never fall back."""
        tx.assert_session(db)
        tx.assert_barrier(tenant_id)
        require(type(role_ids) is tuple and len(role_ids) <= 4096)
        for role_id in role_ids:
            identity(role_id)
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_bytes=128 * 1024 * 1024)
        ):
            await self._catalog(db, catalog)
            head = await self._head(db, tenant_id)
            objects = await self._objects(db, tenant_id, catalog)
            state = next((r for r in objects if r["object_id"] == object_id), None)
            if (
                head["catalog_digest"] != catalog.digest
                or head["rollout_state"] == "preparing"
                or head["active_manifest_id"] is None
                or state is None
                or state["mode"] != "enforcing"
            ):
                raise failure()
            _, items, grants = await read_manifest(
                db,
                tenant_id=tenant_id,
                manifest_id=head["active_manifest_id"],
                expected_digest=head["active_manifest_digest"],
                catalog=catalog,
            )
            expected = digest(
                {
                    "catalog": catalog.digest,
                    "object": object_id,
                    "items": [item for item in items if item["object_id"] == object_id],
                },
                4 * 1024 * 1024,
            )
            if state["active_object_digest"] != expected:
                raise failure()
            return tuple(
                g
                for g in grants
                if g.role_id in role_ids and g.ref.object_id == object_id
            )

    async def _head(self, db, tenant_id):
        row = (
            (
                await db.execute(
                    select(Head.__table__).where(Head.tenant_id == tenant_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise failure()
        return row

    async def _catalog(self, db, catalog):
        row = (
            (
                await db.execute(
                    select(CatalogRow.__table__).where(
                        CatalogRow.catalog_digest == catalog.digest
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise failure()
        try:
            loaded = load_catalog(
                row["canonical_document"], expected_digest=catalog.digest
            )
            if catalog_parts(loaded) != (
                row["action_catalog_digest"],
                row["adapter_digest"],
            ):
                raise failure()
        except BusinessException:
            raise failure() from None
        rows = (
            (
                await db.execute(
                    select(CatalogObject.__table__)
                    .where(CatalogObject.catalog_digest == catalog.digest)
                    .limit(65)
                )
            )
            .mappings()
            .all()
        )
        if {r["object_id"] for r in rows} != set(catalog.objects):
            raise failure()
        for item in rows:
            obj = catalog.objects[item["object_id"]]
            if (
                item["domain_kind"],
                item["schema_digest"],
                item["adapter_version"],
            ) != (obj.domain_kind, digest(primitive(obj)), obj.scope_adapter):
                raise failure()

    async def _objects(self, db, tenant_id, catalog):
        rows = (
            (
                await db.execute(
                    select(ObjectState.__table__)
                    .where(ObjectState.tenant_id == tenant_id)
                    .order_by(ObjectState.object_id)
                    .limit(65)
                )
            )
            .mappings()
            .all()
        )
        expected = {
            obj.object_id
            for obj in catalog.objects.values()
            if obj.domain_kind == "tenant"
        }
        if {r["object_id"] for r in rows} != expected or any(
            r["catalog_digest"] != catalog.digest for r in rows
        ):
            raise failure()
        return rows

    async def _bindings(self, db, tenant_id, catalog):
        b, r = Binding.__table__, Revision.__table__
        rows, last = [], (0, "")
        while True:
            page = (
                (
                    await db.execute(
                        select(b, r.c.rules, r.c.rules_digest, r.c.catalog_digest)
                        .join(
                            r,
                            (b.c.tenant_id == r.c.tenant_id)
                            & (b.c.role_id == r.c.role_id)
                            & (b.c.object_id == r.c.object_id)
                            & (b.c.revision_id == r.c.revision_id),
                        )
                        .where(
                            b.c.tenant_id == tenant_id,
                            tuple_(b.c.role_id, b.c.object_id) > last,
                        )
                        .order_by(b.c.role_id, b.c.object_id)
                        .limit(16)
                    )
                )
                .mappings()
                .all()
            )
            if not page:
                return rows
            for row in page:
                if (
                    row["catalog_digest"] != catalog.digest
                    or digest(row["rules"]) != row["rules_digest"]
                ):
                    raise failure()
                load_rules(
                    catalog,
                    row["rules"],
                    tenant_id=tenant_id,
                    role_id=row["role_id"],
                    object_id=row["object_id"],
                )
                rows.append(row)
            require(len(rows) <= 16384)
            last = (page[-1]["role_id"], page[-1]["object_id"])

    @staticmethod
    def _item_rows(bindings):
        return [
            {
                "role_id": str(row["role_id"]),
                "object_id": row["object_id"],
                "revision_id": str(row["revision_id"]),
                "rules_digest": row["rules_digest"],
            }
            for row in bindings
            if row["archived_at"] is None
        ]

    async def receipt(self, db, *, tenant, request_id, command_digest=None):
        require_tenant_context(tenant)
        request_identity(request_id)
        row = (
            (
                await db.execute(
                    select(Request.__table__).where(
                        Request.tenant_id == tenant.tenant_id,
                        Request.actor_user_id == tenant.actor_user_id,
                        Request.request_id == request_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if command_digest is not None and row["command_digest"] != command_digest:
            raise failure("FIELD_POLICY_REQUEST_CONFLICT", 409)
        if digest(row["receipt"]) != row["receipt_digest"]:
            raise failure()
        return row["receipt"]

    async def plan(self, db, tx, *, tenant, catalog, command, qualification=None):
        require_tenant_context(tenant)
        require(isinstance(db, AuthorizationPlanSession))
        tx.assert_session(db)
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        encoded = command_bytes(command)
        cmd = json.loads(encoded)
        tid = tenant.tenant_id
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_bytes=128 * 1024 * 1024)
        ):
            await self._catalog(db, catalog)
            head = await self._head(db, tid)
            require(head["catalog_digest"] == catalog.digest)
            objects = await self._objects(db, tid, catalog)
            bindings = await self._bindings(db, tid, catalog)
            roles = tuple(
                (
                    await db.scalars(
                        select(Role.role_id)
                        .where(Role.tenant_id == tid)
                        .order_by(Role.role_id)
                        .limit(4097)
                    )
                ).all()
            )
            require(len(roles) <= 4096)
            if "role_id" in cmd:
                require(
                    cmd["role_id"] in roles
                    and cmd["object_id"] in {row["object_id"] for row in objects}
                )
            if cmd["action"] == "publish":
                load_rules(
                    catalog,
                    cmd["rules"],
                    tenant_id=tid,
                    role_id=cmd["role_id"],
                    object_id=cmd["object_id"],
                )
            prior = await self.receipt(
                db,
                tenant=tenant,
                request_id=cmd["request_id"],
                command_digest=digest(cmd),
            )
        existing = {
            key(K.FIELD_POLICY_HEAD, tid, tid),
            key(K.USER, tid, tenant.actor_user_id),
        }
        existing.update(key(K.ROLE, tid, rid) for rid in roles)
        existing.update(
            key(K.FIELD_POLICY_OBJECT, tid, row["object_id"]) for row in objects
        )
        existing.update(
            key(K.FIELD_POLICY_BINDING, tid, (row["role_id"], row["object_id"]))
            for row in bindings
        )
        existing.update(
            key(K.FIELD_POLICY_REVISION, tid, row["revision_id"]) for row in bindings
        )
        for name in ("active_manifest_id", "candidate_manifest_id"):
            if head[name] is not None:
                existing.add(key(K.FIELD_POLICY_MANIFEST, tid, head[name]))
        revision_id, manifest_id, audit_id = next_id(), next_id(), next_id()
        new = []
        if prior is not None:
            existing.add(
                key(
                    K.FIELD_POLICY_REQUEST,
                    tid,
                    (tenant.actor_user_id, cmd["request_id"]),
                )
            )
        else:
            new = [
                NewResource(key(K.FIELD_POLICY_AUDIT, tid, audit_id)),
                NewResource(
                    key(
                        K.FIELD_POLICY_REQUEST,
                        tid,
                        (tenant.actor_user_id, cmd["request_id"]),
                    )
                ),
            ]
            if cmd["action"] == "publish":
                new.append(NewResource(key(K.FIELD_POLICY_REVISION, tid, revision_id)))
                binding_key = key(
                    K.FIELD_POLICY_BINDING, tid, (cmd["role_id"], cmd["object_id"])
                )
                if binding_key not in existing:
                    new.append(NewResource(binding_key))
            if cmd["action"] == "build":
                new.append(NewResource(key(K.FIELD_POLICY_MANIFEST, tid, manifest_id)))
                new.extend(
                    NewResource(
                        key(
                            K.FIELD_POLICY_ITEM,
                            tid,
                            (manifest_id, int(row["role_id"]), row["object_id"]),
                        )
                    )
                    for row in self._item_rows(bindings)
                )
        return GenericPolicyPlan(
            tx,
            tid,
            tenant.actor_user_id,
            catalog.digest,
            encoded,
            LockPlan(tuple(existing), tuple(new)),
            revision_id,
            manifest_id,
            audit_id,
            qualification,
        )

    async def apply(self, db, tx, plan, *, tenant, catalog):
        require(
            type(plan) is GenericPolicyPlan
            and plan.transaction is tx
            and (plan.tenant_id, plan.actor_id, plan.catalog_digest)
            == (tenant.tenant_id, tenant.actor_user_id, catalog.digest)
        )
        tx.assert_session(db)
        tx.assert_held(*plan.lock_plan.existing)
        cmd = json.loads(plan.command)
        prior = await self.receipt(
            db, tenant=tenant, request_id=cmd["request_id"], command_digest=digest(cmd)
        )
        if prior is not None:
            return prior
        with bounded_authorization_reads(
            db, AuthorizationReadBudget(max_bytes=128 * 1024 * 1024)
        ):
            head = await self._head(db, tenant.tenant_id)
            if head["head_version"] != cmd["expected_version"]:
                raise failure("FIELD_POLICY_VERSION_CONFLICT", 409)
            await self._catalog(db, catalog)
            objects = await self._objects(db, tenant.tenant_id, catalog)
            bindings = await self._bindings(db, tenant.tenant_id, catalog)
            for row in bindings:
                tx.assert_held(
                    key(
                        K.FIELD_POLICY_BINDING,
                        tenant.tenant_id,
                        (row["role_id"], row["object_id"]),
                    )
                )
            barrier = (
                (
                    await db.execute(
                        select(Barrier.__table__).where(
                            Barrier.tenant_id == tenant.tenant_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            if "expected_epoch" in cmd and cmd["expected_epoch"] != barrier["epoch"]:
                raise failure("FIELD_POLICY_VERSION_CONFLICT", 409)
            async with tx.generic_policy_change(
                tenant.tenant_id,
                action=cmd["action"],
                actor_user_id=tenant.actor_user_id,
                request_id=cmd["request_id"],
            ):
                changes = dict(READY_FIELDS, head_version=head["head_version"] + 1)
                user_mirror = None
                if cmd["action"] in {"publish", "archive"}:
                    await self._publish(tx, plan, tenant, catalog, cmd, bindings)
                    changes.update(
                        candidate_manifest_id=None, candidate_manifest_digest=None
                    )
                elif cmd["action"] == "build":
                    changes.update(
                        await self._build(
                            tx, plan, tenant, catalog, head, objects, bindings
                        )
                    )
                else:
                    transition, user_mirror = await self._transition(
                        db, tx, plan, tenant, catalog, cmd, head, objects, barrier
                    )
                    changes.update(transition)
                g_epoch, b_epoch = await tx.generic_epoch(
                    expected_epoch=barrier["epoch"], user_mirror=user_mirror
                )
                if cmd["action"] == "ready":
                    changes.update(ready_g_epoch=g_epoch, ready_b_epoch=b_epoch)
                await tx.update_held(
                    key(K.FIELD_POLICY_HEAD, tenant.tenant_id, tenant.tenant_id),
                    changes,
                    expected={"head_version": head["head_version"]},
                )
                result = {
                    "headVersion": changes["head_version"],
                    "epoch": b_epoch,
                    "candidateManifestDigest": changes.get(
                        "candidate_manifest_digest", head["candidate_manifest_digest"]
                    ),
                    "activeManifestDigest": changes.get(
                        "active_manifest_digest", head["active_manifest_digest"]
                    ),
                }
                revision_ref = {}
                if cmd["action"] in {"publish", "archive"}:
                    revision_id = (
                        plan.revision_id
                        if cmd["action"] == "publish"
                        else next(
                            row["revision_id"]
                            for row in bindings
                            if (row["role_id"], row["object_id"])
                            == (cmd["role_id"], cmd["object_id"])
                        )
                    )
                    revision_ref = {
                        "revision_id": revision_id,
                        "role_id": cmd["role_id"],
                        "object_id": cmd["object_id"],
                    }
                    result["revisionId"] = str(revision_id)
                await tx.insert_reserved(
                    key(K.FIELD_POLICY_AUDIT, tenant.tenant_id, plan.audit_id),
                    {
                        "actor_user_id": tenant.actor_user_id,
                        "role_id": cmd.get("role_id"),
                        "object_id": cmd.get("object_id"),
                        "action": cmd["action"],
                        "reason": cmd["reason"],
                        "request_id": cmd["request_id"],
                        "before_digest": head["candidate_manifest_digest"],
                        "after_digest": result["candidateManifestDigest"],
                        "before_version": head["head_version"],
                        "after_version": result["headVersion"],
                        "g_epoch": g_epoch,
                        "b_epoch": b_epoch,
                    },
                )
                await tx.insert_reserved(
                    key(
                        K.FIELD_POLICY_REQUEST,
                        tenant.tenant_id,
                        (tenant.actor_user_id, cmd["request_id"]),
                    ),
                    {
                        "command_digest": digest(cmd),
                        "receipt": result,
                        "receipt_digest": digest(result),
                        "manifest_id": changes.get(
                            "candidate_manifest_id", head["candidate_manifest_id"]
                        ),
                        "manifest_digest": result["candidateManifestDigest"],
                        **revision_ref,
                    },
                )
                return result

    async def _publish(self, tx, plan, tenant, catalog, cmd, bindings):
        current = next(
            (
                row
                for row in bindings
                if (row["role_id"], row["object_id"])
                == (cmd["role_id"], cmd["object_id"])
            ),
            None,
        )
        before = current["binding_version"] if current else 0
        if before != cmd["binding_version"] or (
            cmd["action"] == "archive" and current is None
        ):
            raise failure("FIELD_POLICY_VERSION_CONFLICT", 409)
        binding_key = key(
            K.FIELD_POLICY_BINDING, tenant.tenant_id, (cmd["role_id"], cmd["object_id"])
        )
        if cmd["action"] == "archive":
            changes = {"binding_version": before + 1, "archived_at": datetime.now(UTC)}
        else:
            load_rules(
                catalog,
                cmd["rules"],
                tenant_id=tenant.tenant_id,
                role_id=cmd["role_id"],
                object_id=cmd["object_id"],
            )
            await tx.insert_reserved(
                key(K.FIELD_POLICY_REVISION, tenant.tenant_id, plan.revision_id),
                {
                    "role_id": cmd["role_id"],
                    "object_id": cmd["object_id"],
                    "revision_no": before + 1,
                    "catalog_digest": catalog.digest,
                    "rules": cmd["rules"],
                    "rules_digest": digest(cmd["rules"]),
                    "actor_user_id": tenant.actor_user_id,
                },
            )
            changes = {
                "revision_id": plan.revision_id,
                "binding_version": before + 1,
                "archived_at": None,
            }
        if current is None:
            await tx.insert_reserved(binding_key, changes)
        else:
            await tx.update_held(
                binding_key, changes, expected={"binding_version": before}
            )

    async def _build(self, tx, plan, tenant, catalog, head, objects, bindings):
        items = self._item_rows(bindings)
        document = manifest_document(
            catalog,
            tenant.tenant_id,
            head["active_manifest_digest"],
            [row["object_id"] for row in objects],
            items,
        )
        manifest_digest = digest(document, 4 * 1024 * 1024)
        action, adapter = catalog_parts(catalog)
        await tx.insert_reserved(
            key(K.FIELD_POLICY_MANIFEST, tenant.tenant_id, plan.manifest_id),
            {
                "manifest_version": head["head_version"] + 1,
                "base_active_digest": head["active_manifest_digest"],
                "catalog_digest": catalog.digest,
                "action_catalog_digest": action,
                "adapter_digest": adapter,
                "digest": manifest_digest,
                "expected_item_count": len(items),
                "created_by": tenant.actor_user_id,
            },
        )
        for item in items:
            await tx.insert_reserved(
                key(
                    K.FIELD_POLICY_ITEM,
                    tenant.tenant_id,
                    (plan.manifest_id, int(item["role_id"]), item["object_id"]),
                ),
                {
                    "revision_id": int(item["revision_id"]),
                    "catalog_digest": catalog.digest,
                    "rules_digest": item["rules_digest"],
                },
            )
        await tx.update_held(
            key(K.FIELD_POLICY_MANIFEST, tenant.tenant_id, plan.manifest_id),
            {"status": "sealed"},
            expected={"status": "building"},
        )
        return {
            "candidate_manifest_id": plan.manifest_id,
            "candidate_manifest_digest": manifest_digest,
        }

    async def _transition(
        self, db, tx, plan, tenant, catalog, cmd, head, objects, barrier
    ):
        # Qualification and complete active loading are shared with the runtime reader.

        return await transition(
            self, db, tx, plan, tenant, catalog, cmd, head, objects, barrier
        )


generic_field_policy_service = GenericFieldPolicyService()
