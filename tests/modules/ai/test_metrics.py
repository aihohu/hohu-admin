"""Prometheus 指标埋点测试。

验证：
  - 8 个核心 metric 已注册到 REGISTRY
  - record helper 正确递增 counter / 设置 gauge
  - label 命名遵循 Prometheus 最佳实践（无 user_id / confirmation_id 等高基数）
  - /metrics endpoint 返回 200 + 含指标名

注意：prometheus_client 默认 REGISTRY 是全局的，测试间不清零。
本测试只验证增量，不验证绝对值。
"""

# ruff: noqa: ARG001, PLC0415

from prometheus_client import REGISTRY

from app.modules.ai.metrics import (
    HITL_PENDING_COUNT,
    HITL_PUBSUB_LOST_TOTAL,
    HITL_TIMEOUT_TOTAL,
    HITL_WAKE_TOTAL,
    QUOTA_REJECTED_TOTAL,
    SECURITY_EVENTS_TOTAL,
    TOOL_CALLS_TOTAL,
)


class TestMetricRegistration:
    """8 个核心 metric 都注册到 REGISTRY"""

    def test_all_metrics_registered(self) -> None:
        """八个核心 metric 全部注册到 REGISTRY。

        prometheus_client REGISTRY.collect() 对 Counter 返回去掉 _total 后缀的
        sample name，所以直接断言 sample name 不稳。改为遍历 collect() 后用
        前缀匹配 _total 后缀已被剥离）。
        """
        # prometheus_client collect() 对 Counter 返回带 _total 后缀的 name（不是剥离）
        # 实际从测试输出看：ai_tool_calls / ai_hitl_wake / ai_security_events 等都
        # 是去掉了 _total。所以这里用前缀匹配。
        names = {metric.name for metric in REGISTRY.collect()}
        # Counter 名（_total 在 sample name 已被剥离）
        assert "ai_tool_calls" in names
        assert "ai_hitl_wake" in names
        assert "ai_hitl_pubsub_lost" in names
        assert "ai_hitl_timeout" in names
        assert "ai_quota_rejected" in names
        assert "ai_security_events" in names
        # Histogram / Gauge 名（不被剥离）
        assert "ai_tool_call_duration_seconds" in names
        assert "ai_hitl_pending_count" in names

    def test_tool_calls_total_labels(self) -> None:
        """label 集合冻结：tool / status / risk / execution_mode"""
        labelnames = TOOL_CALLS_TOTAL._labelnames  # type: ignore[attr-defined]
        assert set(labelnames) == {"tool", "status", "risk", "execution_mode"}

    def test_no_high_cardinality_labels(self) -> None:
        """禁止使用 user_id、confirmation_id、tool_call_id 等高基数 label。

        高基数 label 会导致 Prometheus cardinality 爆炸（每用户 / 每调用一行），
        内存吃不消。需要 user 维度的走日志 + trace（OTel 未来加）。
        """
        forbidden = {
            "user_id",
            "confirmation_id",
            "tool_call_id",
            "trace_id",
            "session_id",
        }
        for metric in [
            TOOL_CALLS_TOTAL,
            HITL_PENDING_COUNT,
            HITL_WAKE_TOTAL,
            HITL_TIMEOUT_TOTAL,
            QUOTA_REJECTED_TOTAL,
            SECURITY_EVENTS_TOTAL,
        ]:
            labelnames = set(metric._labelnames)  # type: ignore[attr-defined]
            assert not (labelnames & forbidden), (
                f"metric {metric.name} 含高基数 label: {labelnames & forbidden}"
            )


class TestRecordHelpers:
    """record_* helper 正确递增 counter / 设置 gauge"""

    def test_record_tool_call_increments_counter(self) -> None:
        from app.modules.ai.metrics import record_tool_call

        labels = ("user.list", "success_test", "low", "autonomous")
        before = (
            TOOL_CALLS_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
            if labels in TOOL_CALLS_TOTAL._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_tool_call("user.list", "success_test", "low", "autonomous", 0.123)

        after = TOOL_CALLS_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_security_event_increments_counter(self) -> None:
        from app.modules.ai.metrics import record_security_event

        before = (
            SECURITY_EVENTS_TOTAL.labels("injection_test")._value.get()  # type: ignore[attr-defined]
            if ("injection_test",) in SECURITY_EVENTS_TOTAL._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_security_event("injection_test")

        after = SECURITY_EVENTS_TOTAL.labels("injection_test")._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_quota_rejected_increments_counter(self) -> None:
        from app.modules.ai.metrics import record_quota_rejected

        before = (
            QUOTA_REJECTED_TOTAL.labels("l1_test")._value.get()  # type: ignore[attr-defined]
            if ("l1_test",) in QUOTA_REJECTED_TOTAL._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_quota_rejected("l1_test")

        after = QUOTA_REJECTED_TOTAL.labels("l1_test")._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_hitl_pubsub_lost_increments(self) -> None:
        from app.modules.ai.metrics import record_hitl_pubsub_lost

        before = HITL_PUBSUB_LOST_TOTAL._value.get()  # type: ignore[attr-defined]

        record_hitl_pubsub_lost()

        after = HITL_PUBSUB_LOST_TOTAL._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_hitl_wake_increments(self) -> None:
        from app.modules.ai.metrics import record_hitl_wake

        labels = ("memory_test", "success")
        before = (
            HITL_WAKE_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
            if labels in HITL_WAKE_TOTAL._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_hitl_wake("memory_test", "success")

        after = HITL_WAKE_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_hitl_timeout_increments(self) -> None:
        from app.modules.ai.metrics import record_hitl_timeout

        before = (
            HITL_TIMEOUT_TOTAL.labels("memory_test")._value.get()  # type: ignore[attr-defined]
            if ("memory_test",) in HITL_TIMEOUT_TOTAL._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_hitl_timeout("memory_test")

        after = HITL_TIMEOUT_TOTAL.labels("memory_test")._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

    def test_record_hitl_gauge_inc_dec_pairs(self) -> None:
        """HITL_PENDING_COUNT Gauge 的 inc 和 dec 必须配对。"""
        from app.modules.ai.metrics import record_hitl_created, record_hitl_resolved

        labels = ("memory_test",)
        before = (
            HITL_PENDING_COUNT.labels(*labels)._value.get()  # type: ignore[attr-defined]
            if labels in HITL_PENDING_COUNT._metrics  # type: ignore[attr-defined]
            else 0
        )

        record_hitl_created("memory_test")
        record_hitl_resolved("memory_test")

        after = HITL_PENDING_COUNT.labels(*labels)._value.get()  # type: ignore[attr-defined]
        assert after == before  # inc + dec = 净 0


class TestMetricsEndpoint:
    """/metrics endpoint 返回 200 + 含指标

    不用 TestClient（避免 FastAPI lifespan + DB 全套启动开销，会引入跨测试
    asyncpg teardown flake — 参考 test_confirm.py 注释）。直接调 route
    handler 函数。
    """

    async def test_metrics_endpoint_returns_200_with_metric_names(self) -> None:
        """/metrics 以文本格式暴露所有指标。"""
        from app.main import metrics

        resp = await metrics()

        assert resp.status_code == 200
        assert "text/plain" in resp.media_type

        body = resp.body.decode("utf-8")
        # 至少含一个 AI 指标名（HELP 行）
        assert "ai_tool_calls_total" in body
        assert "ai_hitl_pending_count" in body
        assert "ai_security_events_total" in body

    def test_metrics_endpoint_not_in_openapi(self) -> None:
        """/metrics 用 include_in_schema=False 不进 OpenAPI（内部接口）"""
        from app.main import app

        paths = app.openapi()["paths"]
        assert "/metrics" not in paths
