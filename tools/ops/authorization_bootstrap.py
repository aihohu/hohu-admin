"""Bounded fresh-database checks under the persistent PostgreSQL admission fence."""

import re

from sqlalchemy import text

from app.core.authorization_maintenance import _required

_BASELINE = frozenset(
    {
        "alembic_version",
        "sys_tenant",
        "sys_user_field_policy_global_guard",
        "sys_user_field_policy_state",
        "sys_setting",
        "sys_config",
        "sys_mail_config",
    }
)
_REQUIRED = _BASELINE | {"sys_user", "sys_role", "sys_menu", "sys_platform_principal"}


async def require_fresh_database(db):
    if await db.scalar(text("SELECT current_schema()")) != "public":
        raise _required()
    if await db.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT IN ('public','information_schema') AND n.nspname NOT LIKE 'pg_%' "
            "AND c.relkind IN ('r','p','v','m','f'))"
        )
    ):
        raise _required()
    # Catalog-derived identifiers are strictly validated before quoting. No
    # arbitrary relation is executed as a view, function, or foreign table.
    relations = (
        await db.execute(
            text(
                "SELECT c.relname, c.relkind::text AS relkind FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname='public' "
                "AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname LIMIT 257"
            )
        )
    ).all()
    names = {row.relname for row in relations}
    if (
        len(relations) > 256
        or not _REQUIRED <= names
        or any(
            row.relkind != "r"
            or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", row.relname) is None
            for row in relations
        )
    ):
        raise _required()
    for name in sorted(names - _BASELINE):
        if await db.scalar(
            text('SELECT EXISTS (SELECT 1 FROM public."' + name + '" LIMIT 1)')
        ):
            raise _required()
    if (
        await db.execute(text("SELECT version_num FROM public.alembic_version LIMIT 2"))
    ).all() != [("e0202a000001",)]:
        raise _required()
    if (
        await db.execute(
            text(
                "SELECT tenant_id, tenant_code, status, lifecycle_state, bootstrap_version, row_version "
                "FROM public.sys_tenant LIMIT 2"
            )
        )
    ).all() != [(0, "default", "1", "active", 1, 1)]:
        raise _required()
    if (
        await db.execute(
            text(
                "SELECT guard_id, epoch FROM public.sys_user_field_policy_global_guard LIMIT 2"
            )
        )
    ).all() != [(1, 1)]:
        raise _required()
    if (
        await db.execute(
            text(
                "SELECT tenant_id, mode, ever_enforced, epoch, candidate_manifest_digest, active_manifest_digest "
                "FROM public.sys_user_field_policy_state LIMIT 2"
            )
        )
    ).all() != [(0, "legacy", False, 1, None, None)]:
        raise _required()
    # Migration-owned configuration may be preconfigured, but is not an identity
    # or historical business result. Bound its size without exposing its values.
    for name in ("sys_setting", "sys_config", "sys_mail_config"):
        size = await db.scalar(
            text(
                'SELECT count(*) FROM (SELECT 1 FROM public."'
                + name
                + '" LIMIT 1025) q'
            )
        )
        if size > 1024:
            raise _required()
