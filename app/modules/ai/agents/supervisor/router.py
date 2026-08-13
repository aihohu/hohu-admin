"""LLM-only Supervisor 路由。

候选集 = 当前用户有权限且已启用的 Agent（shared 永远在候选集，作 catch-all）.
LLM 阶段：把候选 Agent 的 name / description 拼进 prompt，返回 agent_code JSON.
JSON 解析必须鲁棒（json.loads → 正则截 {...} → 失败降级）.
"""

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.service.provider_service import provider_service

if TYPE_CHECKING:
    from app.modules.ai.models.agent import AiAgent


_ARROW_JSON_RE = re.compile(r"\{[^{}]*\}")


@dataclass
class RouteResult:
    """Supervisor 路由结果。

    三种状态互斥：
    - agent_code != None + reason='llm_resolved'：路由成功
    - clarification == True：模糊 / 失败，前端弹候选卡片
    - failed == True：候选集空，emit AI_ROUTING_FAILED
    """

    agent_code: str | None = None
    clarification: bool = False
    failed: bool = False
    candidates: list["AiAgent"] = field(default_factory=list)
    reason: str = ""
    llm_raw: str | None = None
    """LLM 原始返回（写入 ai_routing_log.llm_choice 前给审计用）"""


def build_router_prompt(candidates: list["AiAgent"], message: str) -> str:
    """将候选 Agent 名称、描述和用户消息拼成 LLM prompt。"""
    agent_lines = []
    for a in candidates:
        agent_lines.append(f"- {a.code}（{a.name}）: {a.description}")
    agents_block = "\n".join(agent_lines)
    return (
        "你是 HoHu AI 的 Agent 路由器。请根据用户问题，从以下 Agent 中选择最合适的一个。\n"
        '仅返回 JSON（不要 markdown 代码块、不要解释）：{"agent_code": "..."}\n\n'
        f"可选 Agent（按 display_order）：\n{agents_block}\n\n"
        f"用户问题：{message}"
    )


def parse_agent_code_robustly(raw: str, candidates: list["AiAgent"]) -> str | None:
    """容错解析 LLM 返回。

    顺序：
    1. 整段 json.loads
    2. 失败则用正则截首个 {...} 子串重试
    3. 仍失败 / 字段缺失 / code 不在候选 → None
    """
    if not raw:
        return None
    candidate_codes = {a.code for a in candidates}

    def _extract(text: str) -> str | None:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                code = obj.get("agent_code")
                if isinstance(code, str) and code in candidate_codes:
                    return code
        except (json.JSONDecodeError, ValueError):
            return None
        return None

    code = _extract(raw.strip())
    if code:
        return code

    match = _ARROW_JSON_RE.search(raw)
    if match:
        code = _extract(match.group(0))
        if code:
            return code

    return None


async def call_llm_text(model, prompt: str) -> str:
    """使用 PydanticAI Model 执行一次纯文本 completion。

    model 是 provider_service.resolve_model 返回的 PydanticAI Model 实例.
    API（PydanticAI 1.89，参考 app/modules/ai/api/provider.py:242-245）：
      - Agent(model, instructions="...")  # model 是 positional，instructions 是 system prompt
      - agent.run("user_prompt_str")       # 第一参数是 str，不是 messages list
      - result.output                       # 访问输出（默认 str）
    """
    from pydantic_ai import Agent  # noqa: PLC0415

    router_agent = Agent(
        model,
        instructions="你是一个 JSON 路由器，只输出 JSON，不解释。",
    )
    result = await router_agent.run(prompt)
    return result.output


class AgentRouter:
    """LLM-only 路由器。"""

    async def route(
        self,
        db: AsyncSession,
        message: str,
        candidates: list["AiAgent"],
        *,
        model=None,
    ) -> RouteResult:
        if not candidates:
            return RouteResult(failed=True, reason="no_candidates")

        if model is None:
            try:
                model = await provider_service.resolve_model(db, None)
            except Exception:
                return RouteResult(
                    clarification=True,
                    candidates=candidates,
                    reason="no_provider",
                )

        prompt = build_router_prompt(candidates, message)
        try:
            raw = await call_llm_text(model, prompt)
        except Exception:
            return RouteResult(
                clarification=True,
                candidates=candidates,
                reason="llm_call_failed",
            )

        code = parse_agent_code_robustly(raw, candidates)
        if code is None:
            return RouteResult(
                clarification=True,
                candidates=candidates,
                reason="llm_unparsable_or_out_of_scope",
                llm_raw=raw,
            )

        return RouteResult(agent_code=code, reason="llm_resolved", llm_raw=raw)


agent_router = AgentRouter()
