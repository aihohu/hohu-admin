"""Trusted transfer field adapter for compatibility and verified active policies.

All planning runs before domain locks, under the owner's G/B barrier. A policy
provider selects current rules internally; no request supplies rules.
"""

from dataclasses import replace

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.evaluator import (
    FieldGrant,
    resolve_record_fields,
    validate_write_fields,
)
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.field_policy.transfer import (
    EXPORT_COLUMNS,
    export_query_plan,
    import_query_plan,
    import_write_plan,
    project_export,
    transfer_denied,
    validate_lineage,
)
from app.modules.system.field_policy.transfer_catalog import transfer_user_grants
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service import user_export_service as exports
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service,
)
from app.modules.system.service.generic_transfer_export import project_rows, prove_query
from app.modules.system.service.transfer_relation_sources import (
    export_sources,
    recheck_sources,
    require_import_sources,
)
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_compatibility import (
    require_legacy_field_policy,
)
from app.modules.system.service.user_field_context import (
    _limit,
    load_current_user,
    user_field_context,
)
from app.modules.system.service.user_query_coverage import assert_user_query_coverage
from app.utils.data_scope import resolve_data_scope_for_roles


def _rules(role_ids):
    if len(role_ids) > 1024:
        raise _limit()
    rules = []
    for rid in role_ids:
        for name, field in USER_FIELDS.items():
            if field.readable:
                rules.extend(
                    (
                        FieldGrant(rid, name, "read", "management.read"),
                        FieldGrant(rid, name, "export", "export"),
                    )
                )
            if field.query_purposes is None:
                rules.extend(
                    FieldGrant(rid, name, op, "management.query")
                    for op in field.query_ops
                )
            if "exact" in field.query_ops:
                rules.append(FieldGrant(rid, name, "exact", "import"))
            rules.extend(FieldGrant(rid, name, op, "import") for op in field.writable)
    return tuple(rules)


class UserTransferFields:
    def __init__(self, *, legacy_assets=False):
        self.legacy_assets = legacy_assets

    async def context(self, db, tx, *, tenant, version, _after_plan=False):
        tx.assert_session(db)
        tx.assert_barrier(tenant.tenant_id)
        actor = await load_current_user(db, tenant=tenant, actor_user_version=version)
        role_ids = tuple(str(r.role_id) for r in actor.roles if r.status == "1")
        generic = await db.scalar(
            select(FieldPolicyHead.tenant_id).where(
                FieldPolicyHead.tenant_id == tenant.tenant_id
            )
        )
        if generic is None:
            active = await active_policy_grants(
                db, tx, tenant_id=tenant.tenant_id, role_ids=role_ids
            )
        else:
            grants = await generic_field_policy_service.load_active(
                db,
                tx,
                tenant_id=tenant.tenant_id,
                catalog=production_catalog(),
                object_id="system.user",
                role_ids=role_ids,
            )
            active = transfer_user_grants(tenant.tenant_id, grants)
        result = await user_field_context.load(
            db,
            tx,
            tenant=tenant,
            actor_user_version=version,
            grants=_rules(role_ids) if active is None else active,
            _after_plan=_after_plan,
        )
        return replace(result, generic_grants=grants) if generic is not None else result

    async def stamp(self, db, tx, *, tenant_id):
        tx.assert_session(db)
        tx.assert_barrier(tenant_id)
        head = await db.scalar(
            select(FieldPolicyHead)
            .where(FieldPolicyHead.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
        if head is not None:
            catalog = production_catalog()
            await generic_field_policy_service.load_active(
                db,
                tx,
                tenant_id=tenant_id,
                catalog=catalog,
                object_id="system.user",
                role_ids=(),
            )
            state = await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == tenant_id
                )
            )
            global_epoch = await db.scalar(
                select(UserFieldPolicyGlobalGuard.epoch).where(
                    UserFieldPolicyGlobalGuard.guard_id == 1
                )
            )
            if state is None or global_epoch is None:
                raise transfer_denied()
            return {
                "policy_kind": "generic",
                "global_epoch": global_epoch,
                "tenant_epoch": state,
                "mode": "enforcing",
                "active_digest": head.active_manifest_digest,
                "catalog_revision": "generic/2",
                "catalog_digest": catalog.digest,
                "build": settings.RELEASE_BUILD_SHA or "development",
            }
        state = await db.scalar(
            select(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
        global_epoch = await db.scalar(
            select(UserFieldPolicyGlobalGuard.epoch).where(
                UserFieldPolicyGlobalGuard.guard_id == 1
            )
        )
        if (
            state is None
            or global_epoch is None
            or state.catalog_revision != CATALOG_REVISION
            or state.catalog_digest != catalog_digest()
        ):
            raise transfer_denied()
        return {
            "global_epoch": global_epoch,
            "tenant_epoch": state.epoch,
            "mode": state.mode,
            "active_digest": state.active_manifest_digest,
            "catalog_revision": state.catalog_revision,
            "catalog_digest": state.catalog_digest,
            "build": settings.RELEASE_BUILD_SHA or "development",
        }

    async def coverage(
        self, db, tx, context, plan, *, full_match=False, _after_plan=False
    ):
        if context.generic_grants is not None:
            await prove_query(
                db, tx, context, plan, full_match=full_match, _after_plan=_after_plan
            )
            return
        await assert_user_query_coverage(
            db,
            tx,
            tenant_id=context.tenant_id,
            base_predicates=() if full_match else context.base_predicates,
            plan=plan,
            grants=context.grants,
            role_scopes=context.role_scopes,
            _after_plan=_after_plan,
        )

    async def decisions(
        self, db, tx, context, ids, *, operation, purpose, _after_plan=False
    ):
        result = {}
        for offset in range(0, len(ids), 100):
            window = replace(context, _applicable={})
            result.update(
                await window.decisions(
                    db,
                    tx,
                    user_ids=tuple(ids[offset : offset + 100]),
                    operation=operation,
                    purpose=purpose,
                    entrypoint="export" if purpose == "export" else "import",
                    _after_plan=_after_plan,
                )
            )
        return result

    async def export(self, db, tx, *, tenant, version, filters):
        context = await self.context(db, tx, tenant=tenant, version=version)
        await self.coverage(db, tx, context, export_query_plan(filters))
        if context.generic_grants is not None:
            projection, ids = await project_rows(
                db,
                tx,
                tenant=tenant,
                version=version,
                filters=filters,
                context=context,
                fields=self,
            )
            return projection, ids, await self.stamp(db, tx, tenant_id=tenant.tenant_id)
        actor = await load_current_user(db, tenant=tenant, actor_user_version=version)
        rows = await exports._query_users_with_data_scope(
            db, filters, actor, tenant=tenant, max_rows=5001
        )
        if len(rows) > 5000:
            raise exports.UnprocessableEntityException(
                "超过同步导出行数上限，请缩小筛选范围",
                error_code="AI_EXPORT_ASYNC_REQUIRED",
            )
        ids = tuple(row.user_id for row in rows)
        decisions = await self.decisions(
            db, tx, context, ids, operation="export", purpose="export"
        )
        if set(ids) != decisions.keys():
            raise transfer_denied()
        lookup = await exports._build_dept_lookup_for_rows(db, rows, tenant=tenant)
        raw = tuple(
            dict(zip((f for _, f in EXPORT_COLUMNS), values, strict=True))
            for values in exports._excel_values(rows, lookup)
        )
        projection = project_export(raw, tuple(decisions[uid] for uid in ids))
        projection = await export_sources(
            db,
            tx,
            tenant=tenant,
            version=version,
            rows=rows,
            lookup=lookup,
            projection=projection,
        )
        stamp = await self.stamp(db, tx, tenant_id=tenant.tenant_id)
        return projection, ids, stamp

    async def import_context(self, db, tx, *, tenant, version, records):
        context = await self.context(db, tx, tenant=tenant, version=version)
        await self.coverage(
            db,
            tx,
            context,
            import_query_plan(employee_no=any(r.employee_no for r in records)),
            full_match=await db.scalar(
                select(FieldPolicyHead.tenant_id).where(
                    FieldPolicyHead.tenant_id == tenant.tenant_id
                )
            )
            is not None,
        )
        await require_import_sources(
            db, tx, tenant=tenant, version=version, records=records
        )
        return context

    async def import_rows(
        self, db, tx, *, tenant, version, context, records, resolutions, sync_mode
    ):
        by_row = {r.row_num: r for r in resolutions}
        requirements = []
        for record in records:
            resolution = by_row[record.row_num]
            plan = import_write_plan(
                record,
                creating=resolution.target_user_id is None,
                rename=resolution.matched_by_employee_no and sync_mode == "FULL_SYNC",
            )
            requirements.append(
                {
                    "user_id": str(resolution.target_user_id)
                    if resolution.target_user_id
                    else None,
                    "dept_id": str(resolution.dept_id) if resolution.dept_id else None,
                    "operations": {op: sorted(fields) for op, fields in plan.items()},
                }
            )
        await self.import_requirements(
            db,
            tx,
            tenant=tenant,
            version=version,
            context=context,
            requirements=requirements,
        )
        return requirements

    async def import_requirements(
        self, db, tx, *, tenant, version, context, requirements, _after_plan=False
    ):
        ids = tuple(
            sorted(
                {int(r["user_id"]) for r in requirements if r["user_id"] is not None}
            )
        )
        existing = {
            operation: await self.decisions(
                db,
                tx,
                context,
                ids,
                operation=operation,
                purpose="import",
                _after_plan=_after_plan,
            )
            for operation in ("update", "replace")
        }
        actor = await load_current_user(db, tenant=tenant, actor_user_version=version)
        scopes = {
            str(role.role_id): await resolve_data_scope_for_roles(
                db, user=actor, roles=(role,), tenant=tenant
            )
            for role in actor.roles
            if role.status == "1"
        }
        for requirement in requirements:
            dept_id = int(requirement["dept_id"]) if requirement["dept_id"] else None
            for operation, fields in requirement["operations"].items():
                if requirement["user_id"] is None:
                    roles = frozenset(
                        rid
                        for rid, scope in scopes.items()
                        if scope.unbounded
                        or dept_id is not None
                        and scope.accessible_dept_ids is not None
                        and dept_id in scope.accessible_dept_ids
                    )
                    decision = resolve_record_fields(
                        catalog=USER_FIELDS,
                        trusted_grants=context.grants,
                        applicable_role_ids=roles,
                        operation=operation,
                        purpose="import",
                        entrypoint="import",
                    )
                else:
                    decision = existing[operation].get(int(requirement["user_id"]))
                if decision is None:
                    raise AuthorizationException(
                        "无权导入所提供的字段", error_code="FIELD_WRITE_DENIED"
                    )
                validate_write_fields(
                    supplied_paths=fields, decisions=decision, operation=operation
                )

    async def download(
        self,
        db,
        tx,
        *,
        tenant,
        version,
        row,
        identifier,
        kind,
        _after_plan=False,
        _reprove=False,
    ):
        context = await self.context(
            db, tx, tenant=tenant, version=version, _after_plan=_after_plan
        )
        lineage = row.get("field_lineage")
        if lineage is None:
            # Only this explicitly guarded production compatibility path admits
            # historical assets. Strict adapters call validate_lineage directly.
            if self.legacy_assets:
                await require_legacy_field_policy(db, tx, tenant_id=tenant.tenant_id)
                return None
            raise transfer_denied()
        document = lineage.get(kind) if type(lineage) is dict else None
        stamp = await self.stamp(db, tx, tenant_id=tenant.tenant_id)
        original_stamp = document.get("stamp") if type(document) is dict else None
        if _reprove and (
            type(original_stamp) is not dict
            or any(
                original_stamp.get(k) != stamp.get(k)
                for k in ("policy_kind", "catalog_digest", "catalog_revision", "build")
            )
        ):
            raise transfer_denied()
        validate_lineage(
            document,
            tenant_id=tenant.tenant_id,
            owner_id=row["operator_id"],
            resource_id=identifier,
            kind=kind,
            stamp=original_stamp if _reprove else stamp,
        )
        if kind == "export":
            if _reprove:
                await self.coverage(
                    db,
                    tx,
                    context,
                    export_query_plan(
                        exports.UserExportFilter.model_validate(
                            row["filter_snapshot"]["filter"]
                        )
                    ),
                    _after_plan=_after_plan,
                )
            if stamp.get("policy_kind") == "generic":
                await recheck_sources(
                    db,
                    tx,
                    tenant=tenant,
                    version=version,
                    references=document["related_sources"],
                )
            ids = tuple(int(uid) for uid, _ in document["sources"])
            current = await self.decisions(
                db,
                tx,
                context,
                ids,
                operation="export",
                purpose="export",
                _after_plan=_after_plan,
            )
            ranks = {"hidden": 0, "masked": 1, "plain": 2}
            for uid, levels in document["sources"]:
                if int(uid) not in current:
                    raise transfer_denied()
                for field, level in zip(document["columns"], levels, strict=True):
                    if ranks[current[int(uid)].fields[field].read] < ranks[level]:
                        raise transfer_denied()
        elif kind == "preview_source":
            if _reprove:
                await self.coverage(
                    db,
                    tx,
                    context,
                    import_query_plan(
                        employee_no=any(
                            "employee_no" in fields for fields in document["presence"]
                        )
                    ),
                    full_match=stamp.get("policy_kind") == "generic",
                    _after_plan=_after_plan,
                )
            await self.import_requirements(
                db,
                tx,
                tenant=tenant,
                version=version,
                context=context,
                requirements=document["requirements"],
                _after_plan=_after_plan,
            )
        return document


user_transfer_fields = UserTransferFields(legacy_assets=True)
