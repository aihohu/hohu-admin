"""Audit admission failures stop execution; ambiguous writes are never replayed."""

# ruff: noqa: ARG001, PLC0415

from unittest.mock import patch

from tenant_helpers import tenant_context

from app.modules.ai.agents.gateway.executor import LogWriteError


class TestStartLogFailureAbortsExecute:
    """修订 S-15：_start_log 失败时 execute_tool 必须终止，业务不执行"""

    async def test_start_log_failure_returns_internal_error(self) -> None:
        """_start_log 未获得已知提交回执 → execute_tool 返回 AI_INTERNAL_ERROR，业务未执行"""
        from types import SimpleNamespace

        from app.modules.ai.agents.gateway import executor as exec_mod
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools import AiToolMeta
        from app.modules.ai.core.context import ChatDeps, DataScopeContext

        # mock 一个 low-risk tool（避免触发 L1/L2）
        meta = AiToolMeta(
            name="test.start_log_fail",
            agent="test",
            summary="s",
            required_perms=("p",),
            risk="low",
        )

        business_called = {"n": 0}

        async def _business(ctx, **kwargs):
            business_called["n"] += 1
            return {"ok": True}

        class _FakeReg:
            def __init__(self):
                self.meta = meta
                self.fn = _business
                self.dry_run_fn = None

        registered = _FakeReg()

        # 注：直接 mock execute_tool 内部调用链比 setup 完整 Registry 简单
        # 用 patch execute_tool 的关键依赖
        with (
            patch.object(
                exec_mod, "_start_log", side_effect=LogWriteError("simulated")
            ),
            patch.object(exec_mod.ToolRegistry, "get") as mock_get,
        ):
            mock_get.return_value.find.return_value = registered

            tenant = tenant_context(actor_user_id=100)
            deps = ChatDeps(
                user=SimpleNamespace(user_id=100, user_name="alice"),
                perms={"p"},
                db=None,
                data_scope=DataScopeContext(
                    tenant=tenant,
                    accessible_dept_ids=None,
                    accessible_user_scope=None,
                ),
                agent=SimpleNamespace(
                    code="test",
                    enabled=True,
                    daily_quota_per_user=None,
                ),
                trace_id="tr_test_log_fail",
                tenant=tenant,
                conversation_id=1,
            )

            result = await exec_mod.execute_tool("test.start_log_fail", {}, deps)

        assert isinstance(result, ToolResult)
        assert not result.ok
        assert result.error_code == "AI_INTERNAL_ERROR"
        # 业务函数没被调用
        assert business_called["n"] == 0
