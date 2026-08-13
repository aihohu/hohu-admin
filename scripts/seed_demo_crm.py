"""Seed a demo CRM lowcode app for end-to-end testing (multi-model with belongs_to).

Run:
  python scripts/seed_demo_crm.py            # seed (idempotent)
  python scripts/seed_demo_crm.py --remove   # remove all demo data

Creates:
  - mk_app        slug=demo-crm, status=published
  - mk_app_version 1.0.0, review_status=approved, manifest with 2 models (customer + order)
                    where order.customer_id has x-ref → customer
  - mk_tenant_app  status=enabled
  - 2 physical tables: app_data_demo_crm_customer + app_data_demo_crm_order
  - 5 sample customers + 5 sample orders (referencing customers)
  - Redis contributes cache refresh
  - 2 contributes menus (multi-menu support): 客户管理 Demo + 订单管理 Demo

Idempotent: if demo-crm already exists, drops the tables and recreates
the version + sample data.
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
CUSTOMER_TABLE = make_table_name(SLUG, "customer")  # → app_data_demo_crm_customer
ORDER_TABLE = make_table_name(SLUG, "order")  # → app_data_demo_crm_order

MANIFEST = {
    "name": "客户管理 Demo",
    "slug": SLUG,
    "version": "1.0.0",
    "type": "lowcode",
    "category": "business",
    # Multi-model mode uses models[] instead of top-level data_schema.
    # order.customer_id declares x-ref → customer, so the backend auto-joins
    # the customer name into order list responses (decision #79).
    "models": [
        {
            "key": "customer",
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
        },
        {
            "key": "order",
            "data_schema": {
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "integer",
                        "title": "客户",
                        "x-ref": "customer",
                        "x-ref-label": "name",
                    },
                    "amount": {"type": "number", "title": "金额"},
                    "status": {
                        "type": "string",
                        "title": "状态",
                        "enum": ["pending", "paid", "cancelled"],
                        "default": "pending",
                    },
                },
                "required": ["amount"],
            },
        },
    ],
    # Multi-menu (plural `menus`): each app can expose N sidebar entries.
    # Backend contributes_service supports both `menu` (singular, legacy)
    # and `menus` (plural); plural takes precedence when both declared.
    "menus": [
        {
            "title": "客户管理 Demo",
            "icon": "mdi:account-group-outline",
            "order": 100,
            "page_key": "customer_list",
        },
        {
            "title": "订单管理 Demo",
            "icon": "mdi:cart-outline",
            "order": 110,
            "page_key": "order_list",
        },
    ],
    "pages": [
        {
            "key": "customer_list",
            "model": "customer",
            "page_type": "table",
            "title": "客户列表",
        },
        {
            "key": "customer_form",
            "model": "customer",
            "page_type": "form",
            "title": "客户表单",
        },
        {
            "key": "order_list",
            "model": "order",
            "page_type": "table",
            "title": "订单列表",
        },
        {
            "key": "order_form",
            "model": "order",
            "page_type": "form",
            "title": "订单表单",
        },
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

# (customer index, amount, status) — 5 orders, 2 share customer 1 to test dedup
SAMPLE_ORDERS = [
    (0, 12000.50, "paid"),
    (0, 8300.00, "pending"),
    (1, 45000.00, "paid"),
    (2, 1500.00, "cancelled"),
    (4, 9999.99, "pending"),
]


async def _cleanup_existing(db: AsyncSession) -> None:
    """Drop physical tables + delete mk_* rows for SLUG (idempotent reruns)."""
    await db.execute(text(f"DROP TABLE IF EXISTS {ORDER_TABLE}"))
    await db.execute(text(f"DROP TABLE IF EXISTS {CUSTOMER_TABLE}"))
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
    """Create tenant_app (status=enabled) + physical tables for each model."""
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
    for model in MANIFEST["models"]:
        await runner.create_table(
            db,
            table_name=make_table_name(SLUG, model["key"]),
            data_schema=model["data_schema"],
        )


async def _seed_customers(db: AsyncSession) -> list[int]:
    """Insert 5 customers, return their IDs so orders can reference them."""
    now = datetime.now(UTC)
    customer_ids: list[int] = []
    for c in SAMPLE_CUSTOMERS:
        db_id = next_id()
        customer_ids.append(db_id)
        await db.execute(
            text(
                f"INSERT INTO {CUSTOMER_TABLE} "
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
    return customer_ids


async def _seed_orders(db: AsyncSession, customer_ids: list[int]) -> None:
    now = datetime.now(UTC)
    for cust_idx, amount, status in SAMPLE_ORDERS:
        db_id = next_id()
        await db.execute(
            text(
                f"INSERT INTO {ORDER_TABLE} "
                "(id, tenant_id, created_at, updated_at, created_by, updated_by, "
                "customer_id, amount, status) "
                "VALUES (:id, 0, :now, :now, 1, 1, :cid, :amount, :status)"
            ),
            {
                "id": db_id,
                "now": now,
                "cid": customer_ids[cust_idx],
                "amount": amount,
                "status": status,
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

        print("🏗️  Installing (tenant_app + 2 tables)...")
        await _install_and_enable(db, app)
        await db.commit()

        print("👤 Seeding 5 customers...")
        customer_ids = await _seed_customers(db)
        await db.commit()

        print("🛒 Seeding 5 orders (cross-referencing customers)...")
        await _seed_orders(db, customer_ids)
        await db.commit()

        print("🔄 Refreshing contributes cache...")
        await contributes_service.refresh_cache(db, tenant_id=0)

    print()
    print("✅ Demo CRM ready (multi-model with belongs_to).")
    print()
    print("What to test:")
    print("  1. Open http://127.0.0.1:9527 and login")
    print("  2. Sidebar shows TWO menus: '客户管理 Demo' + '订单管理 Demo'")
    print(f"  3. Click '订单管理 Demo' → /app/{SLUG}/order_list")
    print("     → '客户' column shows customer NAME (not raw id) — belongs_to working")
    print("     → 腾讯科技 appears twice (2 orders share customer, batch dedup)")
    print("  4. Click '新增' on orders → '客户' field is a dropdown")
    print("     populated from /app-data/demo-crm/customer")
    print("  5. Select customer + amount → save → returns to list with label")
    print()
    print("Cleanup when done:  python scripts/seed_demo_crm.py --remove")


if __name__ == "__main__":
    asyncio.run(main())
