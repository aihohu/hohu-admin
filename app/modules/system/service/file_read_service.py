"""File metadata and immutable download inputs under the current G/B fence."""

from sqlalchemy import and_, func, select, true

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    NotFoundException,
)
from app.core.field_policy import resolve_fields, validate_write
from app.core.rbac import is_super_admin
from app.modules.system.field_policy.file_sources import (
    FileContentSnapshot,
    require_file_manifest,
)
from app.modules.system.field_policy.transfer_catalog import CONTAINS, CREATE, EQ
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from app.modules.system.schemas.file import FileQuery
from app.modules.system.schemas.file_projection import ProjectedFileOut
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.relation_read_context import (
    RelationReadContext,
    load_relation_read_context,
)

METADATA = (
    "original_name",
    "file_size",
    "file_ext",
    "mime_type",
    "business_type",
    "create_time",
)


class FileContext(RelationReadContext):
    def scope_sql(self, obj, table):
        if obj == "system.file":
            owner = (
                true()
                if is_super_admin(self.actor)
                else table.c.owner_user_id == self.actor.user_id
            )
            return dict.fromkeys(self.scopes, owner)
        return super().scope_sql(obj, table)

    def base(self, obj, table):
        base = super().base(obj, table)
        return and_(base, table.c.del_flag == "0") if obj == "system.file" else base

    def policy(self, record, *, present=None, can_download=False):
        decisions = self.decisions(record)
        create = (
            resolve_fields(self.catalog, self.rules, record, "file.upload", CREATE)
            if set(self.catalog.actions["file.upload"].permission_codes)
            <= self.permissions
            else {}
        )
        allowed_create = sorted(name for name, value in create.items() if value.allowed)
        return {
            "read": sorted(
                name
                for name, value in decisions.items()
                if value.allowed
                and name != "content"
                and (present is None or name in present)
            ),
            "read_levels": {name: value.level for name, value in decisions.items()},
            "query": sorted(
                {
                    use.ref.field_id
                    for use in self.catalog.actions["file.list"].uses
                    if use.access.purpose == "management.query"
                    and resolve_fields(
                        self.catalog, self.rules, record, "file.list", use.access
                    )[use.ref.field_id].allowed
                }
            ),
            "create": allowed_create,
            "update": [],
            "special_create": [],
            "special_update": [],
            "can_create": {"original_name", "content"} <= set(allowed_create),
            "complete": {},
            "can_download": can_download,
        }


async def load_file_context(db, tx, *, tenant, version, action, sources=False):
    context = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        action=action,
        objects={"system.file", *({"system.user"} if sources else set())},
    )
    return FileContext(
        context.tenant,
        context.actor,
        context.rules,
        context.scopes,
        context.permissions,
        context.action,
    )


async def validate_file_create(db, tx, *, tenant, version, names):
    context = await load_file_context(
        db, tx, tenant=tenant, version=version, action="file.upload"
    )
    record = context.record("system.file", "new", context.scopes)
    for _name, kind in names:
        fields = (
            "original_name",
            "content",
            *(("business_type",) if kind is not None else ()),
        )
        validate_write(
            context.catalog, context.rules, record, "file.upload", CREATE, fields
        )
    return context


def content_snapshot(row):
    return FileContentSnapshot(
        row.tenant_id,
        row.file_id,
        row.owner_user_id,
        row.file_path,
        row.file_ext,
        row.mime_type,
        row.file_size,
        row.delivery_class,
        row.content_sha256,
        row.source_manifest,
    )


async def require_file_content(db, tx, *, tenant, version, identifier, action):
    """Caller owns G/B and its full FILE plan; this helper acquires no locks."""
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    generic = await db.scalar(
        select(FieldPolicyHead.tenant_id).where(
            FieldPolicyHead.tenant_id == tenant.tenant_id
        )
    )
    if generic is None:
        return  # The original fixed owner/function protocol remains responsible.
    context = await load_file_context(
        db, tx, tenant=tenant, version=version, action=action
    )
    record = context.record("system.file", identifier, context.scopes)
    if not context.decisions(record)["content"].allowed:
        raise AuthorizationException("无权读取文件内容", error_code="FIELD_READ_DENIED")
    row = await db.scalar(
        select(File)
        .where(
            File.tenant_id == tenant.tenant_id,
            File.file_id == identifier,
            File.owner_user_id == tenant.actor_user_id,
            File.del_flag == "0",
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundException("文件")
    require_file_manifest(content_snapshot(row))


class FileReadService:
    async def metadata(self, db, tx, *, tenant, version, query=None, identifier=None):
        action = "file.list" if query is not None else "file.detail"
        context = await load_file_context(
            db, tx, tenant=tenant, version=version, action=action, sources=True
        )
        if query is not None and (
            type(query) is not FileQuery or query.business_id is not None
        ):
            raise AuthorizationException(
                "查询用途未登记", error_code="FIELD_QUERY_DENIED"
            )
        predicates = []
        if query is not None:
            dependencies = []
            for name, access in (
                ("original_name", CONTAINS),
                ("business_type", EQ),
                ("file_ext", EQ),
            ):
                value = getattr(query, name)
                if value not in (None, ""):
                    dependencies.append((name, access))
                    predicates.append(
                        File.original_name.contains(value, autoescape=True)
                        if name == "original_name"
                        else File.__table__.c[name] == value
                    )
            await context.prove(db, "system.file", File.__table__, tuple(dependencies))
        else:
            predicates.append(File.file_id == identifier)
        total = (
            await db.scalar(
                select(func.pg_catalog.count(1))
                .select_from(File)
                .where(context.base("system.file", File.__table__), *predicates)
            )
            if query is not None
            else None
        )
        records = await context.fetch(
            db,
            File,
            "file_id",
            predicates=tuple(predicates),
            order=(File.file_id.asc(),),
            offset=(query.current - 1) * query.size if query is not None else 0,
            maximum=query.size if query is not None else 1,
            names=METADATA,
            internals=(
                "owner_user_id",
                "delivery_class",
                "content_sha256",
                "source_manifest",
            ),
        )
        if query is None and not records:
            raise NotFoundException("文件")
        owner_ids = {
            facts["owner_user_id"]
            for _, _, facts in records
            if facts["owner_user_id"] is not None
        }
        owners = (
            await context.fetch(
                db,
                User,
                "user_id",
                predicates=(User.user_id.in_(owner_ids),),
                maximum=101,
                names=("user_name",),
            )
            if owner_ids
            else ()
        )
        names = {
            int(record.record_key): values["user_name"]
            for record, values, _ in owners
            if "user_name" in values
        }
        for record, values, _ in owners:
            if "user_name" in values:
                context.source(record, ("user_name",))
        output = []
        for record, values, facts in records:
            decision = context.decisions(record)
            if decision["owner_name"].allowed and facts["owner_user_id"] in names:
                values["owner_name"] = names[facts["owner_user_id"]]
            available = False
            if decision["content"].allowed:
                try:
                    require_file_manifest(
                        FileContentSnapshot(
                            tenant.tenant_id,
                            int(record.record_key),
                            facts["owner_user_id"],
                            "",
                            "",
                            None,
                            0,
                            facts["delivery_class"],
                            facts["content_sha256"],
                            facts["source_manifest"],
                        )
                    )
                    available = True
                except BusinessException:
                    pass
            values.update(
                file_id=record.record_key,
                field_policy=context.policy(
                    record, present=values, can_download=available
                ),
            )
            output.append(
                ProjectedFileOut.model_validate(values).model_dump(
                    by_alias=True, exclude_unset=True
                )
            )
        if query is None:
            return output[0]
        return {
            "records": output,
            "total": total,
            "current": query.current,
            "size": query.size,
            "field_policy": context.policy(
                context.record("system.file", "query", context.scopes)
            ),
        }

    async def download(self, db, tx, *, tenant, version, identifier, expected=None):
        context = await load_file_context(
            db, tx, tenant=tenant, version=version, action="file.download"
        )
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=version,
            permissions=("system:file:list",),
        )
        statement = (
            select(File)
            .where(
                File.tenant_id == tenant.tenant_id,
                File.file_id == identifier,
                context.base("system.file", File.__table__),
            )
            .execution_options(populate_existing=True)
        )
        row = await db.scalar(statement)
        if row is None:
            raise NotFoundException("文件")
        record = context.record("system.file", identifier, context.scopes)
        if not context.decisions(record)["content"].allowed:
            raise AuthorizationException("无权下载文件", error_code="FIELD_READ_DENIED")
        snapshot = require_file_manifest(content_snapshot(row))
        if expected is not None and snapshot != expected:
            raise NotFoundException("文件")
        await tx.lock(
            LockPlan(
                existing=(
                    *identity.keys,
                    ResourceKey(ResourceKind.FILE, tenant.tenant_id, identifier),
                )
            )
        )
        await assert_identity(db, identity)
        fresh = await db.scalar(statement)
        if fresh is None or content_snapshot(fresh) != snapshot:
            raise NotFoundException("文件")
        return snapshot


file_read_service = FileReadService()
