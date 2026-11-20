"""Measured transaction work and fail-closed boundaries, not a production SLA."""

import json
import time

import pytest
from sqlalchemy import insert

from app.core.exceptions import BusinessException
from app.db import authorization_business_owner as owners
from app.modules.system.models.dict_type import DictType
from app.modules.system.schemas.dict_type import DictTypeQuery
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from tests.modules.system.test_authorization_business_owner import business as business
from tests.modules.system.test_authorization_business_owner import make_type
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_measure_ordinary_page_and_write_budget(
    business, monkeypatch, record_property
):
    execute, _, owner = business
    measured = []

    def budget(db):
        result = AuthorizationReadBudget()
        measured.append(result)
        return bounded_authorization_reads(db, result)

    monkeypatch.setattr(owners, "bounded_authorization_reads", budget)
    elapsed = []
    for i in range(10):
        start = time.perf_counter()
        await make_type(execute, f"measured_{i}")
        await owner.read(
            tenant=TENANT,
            actor_user_version=1,
            operation="dict-type.list",
            query=DictTypeQuery(size=100),
        )
        elapsed.append((time.perf_counter() - start) * 1000)
    stats = {
        "queries": max(b.statements for b in measured),
        "rows": max(b.rows for b in measured),
        "decodedBytes": max(b.decoded_bytes for b in measured),
        "cycleP95Ms": sorted(elapsed)[-1],
    }
    assert (
        stats["queries"] < 200
        and stats["rows"] < 10000
        and stats["decodedBytes"] < 8 * 1024 * 1024
    )
    record_property("ordinary_io", json.dumps(stats))


async def test_options_over_limit_fail_without_partial_list(business):
    _, plain, owner = business
    async with plain() as db, db.begin():
        await db.execute(
            insert(DictType),
            [
                {
                    "tenant_id": 0,
                    "dict_type_id": 10000 + i,
                    "dict_name": f"large_{i}",
                    "dict_type": f"large_{i}",
                    "status": "1",
                }
                for i in range(1001)
            ],
        )
    with pytest.raises(BusinessException) as error:
        await owner.read(
            tenant=TENANT, actor_user_version=1, operation="dict-type.options"
        )
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
