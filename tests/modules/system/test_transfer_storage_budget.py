from json import dumps

import pytest
from sqlalchemy import text

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.transfer import require_lineage_storage


def test_aggregate_proofs_cannot_exceed_existing_storage_limit():
    # Each proof fits individually; the actual aggregate must also fit.
    first = {"preview_source": {"sources": ["a" * 1100000]}}
    second = {"import_errors": {"sources": ["b" * 1100000]}}
    assert require_lineage_storage(first) is first
    assert require_lineage_storage(second) is second
    with pytest.raises(AuthorizationException, match="当前字段授权"):
        require_lineage_storage(first | second)


def test_jsonb_spaces_count_before_persistence():
    # Compact JSON fits; PostgreSQL's spaces make the stored envelope exceed 2MiB.
    with pytest.raises(AuthorizationException):
        require_lineage_storage({"export": [0] * 710000})


async def test_unicode_budget_matches_postgres_jsonb(db_session):
    value = {"export": {"schema": "proof", "label": '部门\\"\n', "ids": [1, 2]}}
    assert require_lineage_storage(value) is value
    actual = await db_session.scalar(
        text("SELECT octet_length(CAST(CAST(:value AS jsonb) AS text))"),
        {"value": dumps(value, ensure_ascii=False)},
    )
    assert actual == len(dumps(value, ensure_ascii=False).encode())
