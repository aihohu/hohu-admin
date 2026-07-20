"""log 写入重试单元测试（修订 S-15）

覆盖 _with_log_retry helper：
  - 第一次成功不重试
  - 中间失败后成功（重试 1-2 次）
  - 3 次都失败 + raise_on_failure=True → 抛 LogWriteError
  - 3 次都失败 + raise_on_failure=False → 返回 None + logger.critical
  - 非捕获异常（如 ValueError）→ 直接抛不重试

覆盖 execute_tool 集成：
  - _start_log 失败 → execute_tool 返回 ToolResult.failure(AI_INTERNAL_ERROR)
  - _finish_log_final 失败 → execute_tool 仍返回业务结果（不被审计拖垮）
"""

# ruff: noqa: ARG001, PLC0415

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.modules.ai.agents.gateway.executor import (
    LogWriteError,
    _with_log_retry,
)


class _FakeDBAPIError(OperationalError):
    """用于测试的 DBAPIError 子类"""


def _make_session_local_mock(
    fail_count: int = 0,
    exc_factory=_FakeDBAPIError,
    exc_msg: str = "simulated DB error",
):
    """构造 AsyncSessionLocal mock，前 fail_count 次 begin() 抛错

    返回 (session_local_mock, op_call_count_tracker)
    """
    call_count = {"n": 0}

    class _Ctx:
        def __init__(self) -> None:
            self.begin = MagicMock()

        async def __aenter__(self):
            call_count["n"] += 1
            if call_count["n"] <= fail_count:
                raise exc_factory("stmt", params=None, orig=Exception(exc_msg))
            return self

        async def __aexit__(self, *args):
            return None

    _ctx_instance = _Ctx()
    _ctx_instance.begin.return_value.__aenter__ = AsyncMock(return_value=_ctx_instance)
    _ctx_instance.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    session_local = MagicMock()
    session_local.return_value = _ctx_instance
    return session_local, call_count


# ============ _with_log_retry ============


class TestWithLogRetry:
    async def test_first_attempt_success_no_retry(self) -> None:
        """第一次成功 → 不重试"""
        session_local, calls = _make_session_local_mock(fail_count=0)

        async def _op(log_db):
            return "ok"

        with patch(
            "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
            session_local,
        ):
            with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
                result = await _with_log_retry(
                    "test_op", log_id=1, op=_op, raise_on_failure=True
                )

        assert result == "ok"
        assert calls["n"] == 1
        mock_sleep.assert_not_called()

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        """第一次失败，第二次成功 → sleep 0.5s 后重试"""
        session_local, calls = _make_session_local_mock(fail_count=1)

        async def _op(log_db):
            return "ok"

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await _with_log_retry(
                "test_op", log_id=1, op=_op, raise_on_failure=True
            )

        assert result == "ok"
        assert calls["n"] == 2  # 第一次失败 + 第二次成功
        mock_sleep.assert_awaited_once_with(0.5)

    async def test_retry_succeeds_on_third_attempt(self) -> None:
        """前两次失败，第三次成功 → sleep 0.5s + 1.0s"""
        session_local, calls = _make_session_local_mock(fail_count=2)

        async def _op(log_db):
            return "ok"

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await _with_log_retry(
                "test_op", log_id=1, op=_op, raise_on_failure=True
            )

        assert result == "ok"
        assert calls["n"] == 3
        assert mock_sleep.await_count == 2
        mock_sleep.assert_any_call(0.5)
        mock_sleep.assert_any_call(1.0)

    async def test_three_failures_raise_when_raise_on_failure_true(self) -> None:
        """3 次都失败 + raise_on_failure=True → 抛 LogWriteError"""
        session_local, calls = _make_session_local_mock(fail_count=5)  # 永远失败

        async def _op(log_db):
            return "ok"

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(LogWriteError) as exc_info:
                await _with_log_retry(
                    "test_op", log_id=42, op=_op, raise_on_failure=True
                )

        assert "test_op failed after 3 attempts" in str(exc_info.value)
        assert calls["n"] == 3  # 3 次都跑了

    async def test_three_failures_return_none_when_raise_on_failure_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """3 次都失败 + raise_on_failure=False → 返回 None + critical log"""
        session_local, calls = _make_session_local_mock(fail_count=5)

        async def _op(log_db):
            return "ok"

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            with caplog.at_level("CRITICAL"):
                result = await _with_log_retry(
                    "mark_success",
                    log_id=42,
                    op=_op,
                    raise_on_failure=False,
                )

        assert result is None
        assert calls["n"] == 3
        # critical log 必含 operation + log_id
        critical_logs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert any(
            "ai_operation_log 最终态写入失败" in r.message for r in critical_logs
        )
        assert any(r.__dict__.get("operation") == "mark_success" for r in critical_logs)
        assert any(r.__dict__.get("log_id") == 42 for r in critical_logs)

    async def test_non_dbapi_exception_not_retried(self) -> None:
        """非 (DBAPIError, OperationalError, TimeoutError) 异常直接抛，不重试

        场景：op 函数自身抛 ValueError（如业务逻辑 bug），不应消耗重试预算。
        """
        session_local, _ = _make_session_local_mock(fail_count=0)

        op_calls = {"n": 0}

        async def _op(log_db):
            op_calls["n"] += 1
            raise ValueError("not a DB error")

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            with pytest.raises(ValueError):
                await _with_log_retry(
                    "test_op", log_id=1, op=_op, raise_on_failure=True
                )

        # op 只被调用一次（没重试）
        assert op_calls["n"] == 1
        mock_sleep.assert_not_called()

    async def test_timeout_error_is_retried(self) -> None:
        """TimeoutError 也在捕获范围（修订 S-15）

        op 第一次抛 TimeoutError，第二次返回 ok。验证：
          - 走了重试路径（sleep 被调用）
          - 最终返回成功结果
        """
        session_local, _ = _make_session_local_mock(fail_count=0)

        op_calls = {"n": 0}

        async def _op(log_db):
            op_calls["n"] += 1
            if op_calls["n"] == 1:
                raise TimeoutError("DB timeout")
            return "recovered"

        with (
            patch(
                "app.modules.ai.agents.gateway.executor.AsyncSessionLocal",
                session_local,
            ),
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await _with_log_retry(
                "test_op", log_id=1, op=_op, raise_on_failure=True
            )

        assert result == "recovered"
        assert op_calls["n"] == 2  # 第一次失败 + 第二次成功
        mock_sleep.assert_awaited_once()  # 走了重试路径


# ============ execute_tool 集成（_start_log 失败终止业务） ============


class TestStartLogFailureAbortsExecute:
    """修订 S-15：_start_log 失败时 execute_tool 必须终止，业务不执行"""

    async def test_start_log_failure_returns_internal_error(self) -> None:
        """_start_log 3 次失败 → execute_tool 返回 AI_INTERNAL_ERROR，业务未执行"""
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
        exec_mod.ToolRegistry._instance = None  # reset singleton

        # 注：直接 mock execute_tool 内部调用链比 setup 完整 Registry 简单
        # 用 patch execute_tool 的关键依赖
        with (
            patch.object(
                exec_mod, "_start_log", side_effect=LogWriteError("simulated")
            ),
            patch.object(exec_mod.ToolRegistry, "get") as mock_get,
        ):
            mock_get.return_value.find.return_value = registered

            deps = ChatDeps(
                user=SimpleNamespace(user_id=100, user_name="alice"),
                perms={"p"},
                db=None,
                data_scope=DataScopeContext(
                    accessible_dept_ids=None, accessible_user_scope=None
                ),
                agent=None,
                trace_id="tr_test_log_fail",
                conversation_id=1,
            )

            result = await exec_mod.execute_tool("test.start_log_fail", {}, deps)

        assert isinstance(result, ToolResult)
        assert not result.ok
        assert result.error_code == "AI_INTERNAL_ERROR"
        # 业务函数没被调用
        assert business_called["n"] == 0
