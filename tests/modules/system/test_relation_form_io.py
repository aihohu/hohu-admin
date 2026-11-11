"""Ten real owner reads, with synthetic test qualification, not deployment proof."""

import json
from time import perf_counter

from rich.console import Console
from sqlalchemy import insert

from app.db import authorization_relation_read_owner as reader
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.menu import Menu
from app.modules.system.schemas.relation_form import RelationFormQuery
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def test_ten_active_menu_form_phase_samples(system_sessions, monkeypatch):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0, menu_id=3100, menu_name="Page", menu_type="C", status="1"
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=3101,
                parent_id=3100,
                menu_name="Read",
                permission="app:sample:read",
                menu_type="F",
                status="1",
            )
        )
        await db.commit()
    await activate_relations(system_sessions, monkeypatch)
    budgets = []
    budget_type = reader.AuthorizationReadBudget

    def capture(**kwargs):
        budget = budget_type(**kwargs)
        budgets.append(budget)
        return budget

    monkeypatch.setattr(reader, "AuthorizationReadBudget", capture)
    runtime = AuthorizationRuntime(plain.kw["bind"])
    samples = []
    for _ in range(10):
        start = perf_counter()
        result = await runtime.relation_read(
            tenant=TENANT,
            actor_user_version=1,
            operation="menu.form",
            query=RelationFormQuery(mode="update", record_id="3100"),
        )
        budget = budgets[-1]
        assert result["record"]["buttons"] == [
            {"desc": "Read", "code": "app:sample:read"}
        ]
        assert result["fieldPolicy"]["complete"]["buttons"]
        assert sum(budget.phase_statements.values()) == budget.statements
        assert sum(budget.phase_rows.values()) == budget.rows
        assert sum(budget.phase_bytes.values()) == budget.decoded_bytes
        assert budget.phase_statements["source"] == 2
        samples.append(
            {
                "elapsed_ms": round((perf_counter() - start) * 1000, 3),
                "selects": budget.statements,
                "rows": budget.rows,
                "decoded_estimate_bytes": budget.decoded_bytes,
                "phase_selects": budget.phase_statements,
                "phase_rows": budget.phase_rows,
                "phase_decoded_estimate_bytes": budget.phase_bytes,
            }
        )
    Console(soft_wrap=True, markup=False, highlight=False).print(
        json.dumps(
            {
                "form_io": "menu.form/active-seven/synthetic-test-qualification",
                "dataset": {
                    "actor_users": 1,
                    "actor_roles": 1,
                    "page": 1,
                    "child_buttons": 1,
                },
                "samples": samples,
                "includes_commit_ack": False,
                "p95_nearest_rank_ms": sorted(s["elapsed_ms"] for s in samples)[9],
            }
        )
    )
