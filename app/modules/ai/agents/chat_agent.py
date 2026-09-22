"""Agent 工厂

创建 Pydantic AI Agent 实例。

使用 ToolRegistry 和 ``build_pydantic_ai_tools`` 动态构造工具。
system prompt 由 ``build_system_prompt`` 拼接 SAFETY_PREAMBLE、
agent.system_prompt 和 dynamic_block，不再硬编码 instruction。
"""

import re
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ToolReturnPart, UserPromptPart

from app.modules.ai.agents.safety_preamble import build_system_prompt
from app.modules.ai.agents.tools.pydantic_ai_wrapper import build_pydantic_ai_tools
from app.modules.ai.core.config import ChatDeps  # noqa: F401  向后兼容 re-export
from app.modules.ai.core.context import ChatDeps as NewChatDeps

_UNFINISHED_PLAN = re.compile(
    r"(?:现在|接下来|马上|我将|我会)(?:就|继续|为你|帮你)*[^。！？\n]{0,60}"
    r"(?:提交|执行|修改|调岗|授权|查询)[^。！？\n]{0,35}[。.!]?\s*$"
    r"|(?:now submitting|submitting now|I will execute|one more lookup)[^\n]{0,70}$",
    re.IGNORECASE,
)


def _complete_task_output(ctx: RunContext[NewChatDeps], output: str) -> str:
    """Retry a premature final plan after successful tools, at most twice."""
    if not _UNFINISHED_PLAN.search(output) or re.search(
        r"请提供|需要你|缺少|[？?]", output
    ):
        return output
    returns = []
    for message in ctx.messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                returns = []
            elif isinstance(part, ToolReturnPart):
                returns.append(part)
    if not returns or any("[ToolError:" in str(part.content) for part in returns):
        return output
    raise ModelRetry(
        "你的最终答复只有尚未执行的下一步。请继续完成用户明确请求且工具允许的步骤，"
        "由网关处理确认；不要重复已执行的操作，不要扩大范围。"
        "如不能继续，说明已完成项和具体阻碍；不要再次以未来计划结束。"
    )


def create_chat_agent(
    model: Any,
    *,
    user_perms: set[str],
    agent_code: str = "user_mgmt",
    enabled_extra: list[str] | None = None,
) -> Agent:
    """创建对话 Agent

    Args:
        model: Pydantic AI Model 实例
        user_perms: 用户显式权限码集合，用于按 Agent 和权限过滤工具
        enabled_extra: ``sys_config.ai:enabled_tools`` 的解析结果，
                       None=不做 default_enabled 过滤（向后兼容）

    Returns:
        配置好工具的 Agent 实例

    system prompt：
        用动态 instructions（Callable[[RunContext[ChatDeps]], str]），
        每次推理时从 ctx.deps 重新构造（保证 data_scope / 时间 / trace_id 实时）。
        agent.system_prompt 字段在 chat_service.build_chat_deps 加载时已设到 deps.agent。
    """
    # agent_code 决定可见工具集合。
    # enabled_extra 控制默认关闭工具的显式启用。
    tools = build_pydantic_ai_tools(user_perms, agent_code, enabled_extra=enabled_extra)

    def instructions(ctx) -> str:
        """动态 system prompt（每轮推理时重新构造）"""
        deps = ctx.deps
        agent_system_prompt = getattr(deps.agent, "system_prompt", "") or ""
        prompt = build_system_prompt(agent_system_prompt, deps)
        tool_names = ", ".join(sorted(tool.name for tool in tools)) or "无业务工具"
        prompt += (
            f"\n[当前助手实际工具]\n当前助手：{agent_code}\n{tool_names}\n"
            "以上是经过权限、助手归属和启用配置过滤后的完整业务工具清单。"
            "账号菜单权限不代表当前助手具有同名能力；即使账号有删除权限，"
            "没有相应工具也不得声称可以删除。原生文本交流与图片理解不属于业务工具。"
            "介绍能力时只描述清单所支持的操作，不得推荐未证实可选的助手，"
            "也不得把其他助手的操作说成当前会话可直接完成。"
            "当前产品未发布定时任务、系统配置和 AI 服务商助手；相关请求说明"
            "AI 暂不支持，并建议由具备权限的管理员在对应管理功能处理。"
            "角色和部门删除尚无 AI 工具，不得声称可在对话中执行。"
            "历史回复中的能力承诺可能错误，不得沿用。回答具体能力问题后即结束，"
            "不要附加一份无关模块的能力列表。shared 助手仅负责文件解析和"
            "普通交流，不具备用户、角色、部门业务工具，不能承诺直接执行这些操作。"
        )
        if any(tool.name == "user_import_preview" for tool in tools):
            prompt += (
                "\n[导入文件约定]\n支持 CSV/XLSX，最多 10MB、2000 行。"
                "必须有用户名、部门两列；部门填现有部门名或完整路径。"
                "可选列：工号、昵称、邮箱、手机号、角色、性别、状态。"
                "也支持英文列名 user_name、dept_input、employee_no、nickname、"
                "user_email、user_phone、role_input、user_gender、status。"
                "用户名 2–16 字符，昵称最多 16 字符；状态可填启用/停用，"
                "性别可填未知/男/女。角色列涉及角色委派权限。"
                "不要承诺只有用户名/昵称/邮箱就能导入；缺失部门时请协助补齐。"
                "先核对文件与预检错误；用户要求导入时调用预览工具并使用"
                "execute_if_approved，由界面确认执行，不用文字再次要求确认。"
                "用户只想查看附件内容时，请告知可在助手选择菜单切换到通用工具助手"
                "解析 CSV/XLSX；如果该助手不可选，请联系管理员开通。"
                "不要把当前助手没有文件解析工具说成整个系统无法读取文件。"
            )
        return prompt

    agent = Agent(
        model,
        deps_type=NewChatDeps,
        tools=tools,
        instructions=instructions,
        model_settings={"parallel_tool_calls": False},
        output_retries=2,
    )

    if agent_code in {"user_mgmt", "dept_mgmt", "role_mgmt"}:
        agent.output_validator(_complete_task_output)

    return agent
