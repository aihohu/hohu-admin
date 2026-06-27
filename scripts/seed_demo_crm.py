"""Seed a demo CRM lowcode app for end-to-end MVP testing.

Run:
  python scripts/seed_demo_crm.py            # seed (idempotent)
  python scripts/seed_demo_crm.py --remove   # remove all demo data

Creates:
  - mk_app        slug=demo-crm, status=published
  - mk_app_version 1.0.0, review_status=approved, manifest with data_schema + menu + pages
  - mk_tenant_app  status=enabled
  - physical table app_data_demo_crm (single-table mode)
  - 5 sample customer rows
  - Redis contributes cache refresh

Idempotent: if demo-crm already exists, drops the table and recreates
the version + sample data (preserves the app row + tenant_app).
"""

import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.marketplace.lowcode.migration_runner import MigrationRunner
from app.modules.marketplace.lowcode.type_mapping import make_table_name
from app.modules.marketplace.models import App, AppVersion, TenantApp
from app.modules.marketplace.service.contributes_service import contributes_service
from app.modules.system.models.user import (
    User,  # noqa: F401  -- register sys_user for FK resolution
)

SLUG = "demo-crm"
TABLE_NAME = make_table_name(SLUG)  # → app_data_demo_crm

MANIFEST = {
    "name": "客户管理 Demo",
    "slug": SLUG,
    "version": "1.0.0",
    "type": "lowcode",
    "category": "business",
    "data_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 100, "title": "客户名称"},
            "level": {
                "type": "string",
                "title": "等级",
                "enum": ["A", "B", "C"],
                "default": "C",
            },
            "contact": {"type": "string", "title": "联系方式"},
            "age": {"type": "integer", "title": "年龄"},
            "tags": {"type": "array", "title": "标签"},
        },
        "required": ["name", "level"],
    },
    "menu": {
        "title": "客户管理 Demo",
        "icon": "PeopleOutline",
        "order": 100,
        "page_key": "list",
    },
    "pages": [
        {"key": "list", "page_type": "table", "title": "客户列表"},
        {"key": "form", "page_type": "form", "title": "客户表单"},
    ],
}

SAMPLE_CUSTOMERS = [
    {
        "name": "腾讯科技",
        "level": "A",
        "contact": "138-0001-2345",
        "age": 22,
        "tags": ["vip", "战略"],
    },
    {
        "name": "阿里巴巴",
        "level": "A",
        "contact": "139-0002-3456",
        "age": 25,
        "tags": ["vip"],
    },
    {
        "name": "字节跳动",
        "level": "B",
        "contact": "137-0003-4567",
        "age": 12,
        "tags": ["normal"],
    },
    {
        "name": "美团点评",
        "level": "B",
        "contact": "136-0004-5678",
        "age": 15,
        "tags": ["normal"],
    },
    {
        "name": "小米科技",
        "level": "C",
        "contact": "135-0005-6789",
        "age": 30,
        "tags": [],
    },
]


async def _cleanup_existing(db: AsyncSession) -> None:
    """Drop physical table + delete mk_* rows for SLUG (idempotent reruns)."""
    await db.execute(text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
    existing = (
        await db.execute(select(App).where(App.slug == SLUG))
    ).scalar_one_or_none()
    if existing:
        await db.execute(delete(TenantApp).where(TenantApp.app_id == existing.id))
        await db.execute(delete(AppVersion).where(AppVersion.app_id == existing.id))
        await db.execute(delete(App).where(App.id == existing.id))


async def _create_app_and_version(db: AsyncSession) -> App:
    app = App(
        id=next_id(),
        tenant_id=0,
        name=MANIFEST["name"],
        slug=SLUG,
        type="lowcode",
        category="business",
        status="published",
    )
    db.add(app)
    await db.flush()

    version = AppVersion(
        id=next_id(),
        app_id=app.id,
        version="1.0.0",
        manifest=MANIFEST,
        file_url="/uploads/demo-crm.zip",
        file_hash="0" * 64,
        file_size=1024,
        review_status="approved",
    )
    db.add(version)
    await db.flush()

    app.current_version_id = version.id
    await db.flush()
    return app


async def _install_and_enable(db: AsyncSession, app: App) -> None:
    """Create tenant_app (status=enabled) + physical table via MigrationRunner."""
    tenant_app = TenantApp(
        id=next_id(),
        tenant_id=0,
        app_id=app.id,
        installed_version="1.0.0",
        status="enabled",
        installed_at=datetime.now(UTC),
    )
    db.add(tenant_app)
    await db.flush()

    runner = MigrationRunner()
    await runner.create_table(
        db,
        table_name=TABLE_NAME,
        data_schema=MANIFEST["data_schema"],
    )


async def _seed_customers(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    for c in SAMPLE_CUSTOMERS:
        db_id = next_id()
        await db.execute(
            text(
                f"INSERT INTO {TABLE_NAME} "
                "(id, tenant_id, created_at, updated_at, created_by, updated_by, "
                "name, level, contact, age, tags) "
                "VALUES (:id, 0, :now, :now, 1, 1, "
                ":name, :level, :contact, :age, CAST(:tags AS JSONB))"
            ),
            {
                "id": db_id,
                "now": now,
                "name": c["name"],
                "level": c["level"],
                "contact": c["contact"],
                "age": c["age"],
                "tags": json.dumps(c["tags"], ensure_ascii=False),
            },
        )


async def main() -> None:
    remove_only = "--remove" in sys.argv

    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        print(f"🧹 Cleaning up existing '{SLUG}' if present...")
        await _cleanup_existing(db)
        await db.commit()
        await contributes_service.invalidate(tenant_id=0)

        if remove_only:
            print()
            print("✅ Demo CRM removed.")
            return

        print("📦 Creating app + version...")
        app = await _create_app_and_version(db)
        await db.commit()

        print("🏗️  Installing (tenant_app + table)...")
        await _install_and_enable(db, app)
        await db.commit()

        print("👤 Seeding 5 sample customers...")
        await _seed_customers(db)
        await db.commit()

        print("🔄 Refreshing contributes cache...")
        await contributes_service.refresh_cache(db, tenant_id=0)

    print()
    print("✅ Demo CRM ready.")
    print()
    print("What to test:")
    print("  1. Open http://127.0.0.1:9527 and login (admin / your password)")
    print("  2. Sidebar should show '客户管理 Demo' under contributes")
    print(f"  3. Click → goes to /app/{SLUG}/list → TablePage renders")
    print("  4. Try filters: name__contains=腾, level=A, age__gte=20")
    print("  5. Click column header → sort")
    print("  6. Click 新增 → leave name empty → required validation fires")
    print("  7. Fill name + level → save → returns to list")
    print()
    print("Cleanup when done:  python scripts/seed_demo_crm.py --remove")


if __name__ == "__main__":
    asyncio.run(main())
