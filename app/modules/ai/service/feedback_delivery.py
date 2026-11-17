"""Current source projections and complete-domain feedback statistics."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.field_policy.contracts import denied, limit
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.ai.schemas.routing_feedback import FeedbackListQuery
from app.modules.system.field_policy.delivery_catalog import (
    AGGREGATE,
    EQ,
    GROUP,
    RANGE,
    SORT,
)
from app.modules.system.schemas.delivery_projection import ProjectedFeedback
from app.modules.system.service.delivery_read_context import load_delivery_context


async def feedback_page(db, tx, *, tenant, version, query=None, days=None):
    tx.assert_planning(tenant.tenant_id, exclusive=True)
    summary = query is None
    if (summary and (type(days) is not int or not 1 <= days <= 365)) or (
        not summary and type(query) is not FeedbackListQuery
    ):
        raise denied()
    days = days if summary else query.days
    context = await load_delivery_context(
        db,
        tx,
        tenant=tenant,
        version=version,
        action="delivery.feedback-list",
        agents=True,
    )
    obj, model = "ai.routing_feedback", AiRoutingFeedback
    table = model.__table__
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    context.baselines[obj] = (table.c.create_time >= cutoff,)
    dependencies = [("create_time", RANGE), ("create_time", SORT)]
    if summary:
        dependencies.extend((("feedback", AGGREGATE), ("feedback", GROUP)))
    elif query.feedback != "all":
        dependencies.append(("feedback", EQ))
    for name in ("original_agent", "corrected_agent"):
        if not summary and getattr(query, name):
            dependencies.append((name, EQ))
    await context.prove(
        db, obj, table, tuple(dependencies), readable=("feedback",) if summary else ()
    )
    agents = await context.rows(
        db, "ai.agent", AiAgent, "agent_id", names={"code", "name"}, maximum=1025
    )
    limit(len(agents) <= 1024)
    agent_names = {
        values["code"]: values["name"] for _, values, _ in agents if "name" in values
    }
    predicates = []
    if not summary:
        if query.feedback != "all":
            predicates.append(table.c.feedback == query.feedback)
        for name in ("original_agent", "corrected_agent"):
            value = getattr(query, name)
            if not value:
                continue
            # Name aliases are current visible labels only. Unknown historical
            # codes cannot be used as a query oracle through another source.
            source_codes = set(
                await db.scalars(
                    select(table.c[name]).where(
                        context.base(obj, table), table.c[name].is_not(None)
                    )
                )
            )
            if not source_codes <= agent_names.keys():
                raise denied()
            matches = {
                code
                for code, label in agent_names.items()
                if code == value or label == value
            }
            predicates.append(table.c[name].in_(matches))
    if summary:
        rows = await context.rows(
            db,
            obj,
            model,
            "feedback_id",
            names={"feedback", "original_agent", "corrected_agent"},
            order=(table.c.feedback_id,),
            maximum=10001,
        )
        limit(len(rows) <= 10000)
        statuses = [values["feedback"] for _, values, _ in rows]
        counts = Counter(statuses)
        total = len(statuses)
        result = {
            "days": days,
            "total": total,
            "correct": counts["correct"],
            "wrong": counts["wrong"],
            "wrongRate": round(counts["wrong"] / total, 4) if total else 0.0,
        }
        grouped = defaultdict(list)
        all_sources = True
        for _, values, _ in rows:
            if (
                not {"original_agent", "corrected_agent"} <= values.keys()
                or values["original_agent"] not in agent_names
                or (
                    values["corrected_agent"] is not None
                    and values["corrected_agent"] not in agent_names
                )
            ):
                all_sources = False
                break
            if values["feedback"] == "wrong":
                grouped[values["original_agent"]].append(values["corrected_agent"])
        if all_sources:
            await context.prove(
                db, obj, table, (("original_agent", GROUP), ("corrected_agent", GROUP))
            )
            tops = []
            for code, corrections in sorted(
                grouped.items(), key=lambda item: (-len(item[1]), item[0])
            )[:10]:
                top = sorted(
                    Counter(corrections).items(), key=lambda item: (-item[1], item[0])
                )
                corrected = (
                    {
                        "code": top[0][0],
                        "name": agent_names[top[0][0]],
                        "count": top[0][1],
                    }
                    if top
                    else None
                )
                tops.append(
                    {
                        "agentCode": code,
                        "agentName": agent_names[code],
                        "wrongCount": len(corrections),
                        "topCorrected": corrected,
                    }
                )
            result["topWrongAgents"] = tops
        return result
    total = await db.scalar(
        select(func.pg_catalog.count(1))
        .select_from(table)
        .where(context.base(obj, table), *predicates)
    )
    rows = await context.rows(
        db,
        obj,
        model,
        "feedback_id",
        predicates=tuple(predicates),
        order=(table.c.feedback_id.desc(),),
        offset=(query.current - 1) * query.size,
        maximum=query.size,
        internals=("user_id",),
    )
    user_names = await context.user_names(
        db, (extra["user_id"] for _, _, extra in rows)
    )
    records = []
    for record, values, extra in rows:
        values["feedback_id"] = str(values["feedback_id"])
        if (
            context.decisions(record)["user_name"].allowed
            and extra["user_id"] in user_names
        ):
            values["user_name"] = user_names[extra["user_id"]]
        else:
            values.pop("user_name", None)
        for name in ("original_agent", "corrected_agent"):
            if name in values and values[name] in agent_names:
                values[name + "_name"] = agent_names[values[name]]
            elif values.get(name) is not None:
                values.pop(name, None)
        values["field_policy"] = context.policy(record)
        records.append(
            ProjectedFeedback.model_validate(values).model_dump(
                mode="json", by_alias=True
            )
        )
    return {
        "records": records,
        "total": total or 0,
        "current": query.current,
        "size": query.size,
        "fieldPolicy": context.policy(context.record(obj, "query", context.scopes)),
    }
