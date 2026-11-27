"""AI-owned reconciliation of published built-ins without overwriting overrides."""

from sqlalchemy import select

from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.authorization_seed_plan import SeedChange, SeedPlan
from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.modules.ai.constants import PUBLISHED_AGENT_CODES
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.seed_prompts import DEFAULT_PROMPTS, should_update_prompt
from scripts.seed_ai_agents import AGENT_SEED


class AiSeedCandidate:
    async def plan(self, db):
        rows = [
            dict(row)
            for row in (
                await db.execute(
                    select(AiAgent.__table__).order_by(AiAgent.agent_id).limit(10001)
                )
            ).mappings()
        ]
        if len(rows) > 10000:
            raise BusinessException(
                422, "种子资源超过预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            )
        existing = tuple(
            ResourceKey(ResourceKind.AGENT, None, (row["agent_id"],)) for row in rows
        )
        by_code = {row["code"]: row for row in rows}
        changes = []
        for seed in AGENT_SEED:
            row = by_code.get(seed["code"])
            if row is None:
                values = dict(
                    seed,
                    agent_id=next_id(),
                    i18n_keys={
                        name: f"builtin.agent.{seed['code']}.{name}"
                        for name in ("name", "description")
                    },
                    enabled=seed["code"] in PUBLISHED_AGENT_CODES,
                    is_builtin=True,
                    system_prompt=DEFAULT_PROMPTS.get(seed["code"], ""),
                    model_preference=None,
                )
                changes.append(
                    SeedChange(
                        "insert",
                        ResourceKey(ResourceKind.AGENT, None, (values["agent_id"],)),
                        values,
                    )
                )
                by_code[seed["code"]] = values
                continue
            desired = {"display_order": seed["display_order"]}
            if row["i18n_keys"] is None:
                desired["i18n_keys"] = {
                    name: f"builtin.agent.{seed['code']}.{name}"
                    for name in ("name", "description")
                    if row[name] == seed[name]
                }
            if (
                row["is_builtin"]
                and seed["code"] in DEFAULT_PROMPTS
                and should_update_prompt(
                    seed["code"], row["system_prompt"], force=False
                )
            ):
                desired["system_prompt"] = DEFAULT_PROMPTS[seed["code"]]
            difference = {
                name: value for name, value in desired.items() if row[name] != value
            }
            if difference:
                changes.append(
                    SeedChange(
                        "update",
                        ResourceKey(ResourceKind.AGENT, None, (row["agent_id"],)),
                        difference,
                    )
                )
                by_code[seed["code"]] = row | difference
        published = tuple(
            row["agent_id"]
            for row in by_code.values()
            if row["code"] in PUBLISHED_AGENT_CODES and row["enabled"]
        )
        return SeedPlan(existing, tuple(changes)), published

    async def apply(self, tx, plan):
        for change in plan.changes:
            if change.action == "insert":
                await tx.insert_reserved(change.key, change.values)
            else:
                await tx.update_held(change.key, change.values)


ai_seed_candidate = AiSeedCandidate()
