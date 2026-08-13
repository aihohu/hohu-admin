"""ai_routing_log 表 schema 验证。"""

# ruff: noqa: PLC0415


def test_routing_log_table_exists():
    from app.modules.ai.models.routing_log import AiRoutingLog

    assert AiRoutingLog.__tablename__ == "ai_routing_log"


def test_routing_log_required_columns():
    from app.modules.ai.models.routing_log import AiRoutingLog

    cols = AiRoutingLog.__table__.columns
    assert "log_id" in cols
    assert "trace_id" in cols
    assert "user_id" in cols
    assert "conversation_id" in cols
    assert "input_message_hash" in cols
    assert "candidates" in cols
    assert "llm_choice" in cols
    assert "final_agent" in cols
    assert "reason" in cols
    assert "latency_ms" in cols
    assert "parent_log_id" in cols
    assert "plan_step_index" in cols
    assert "create_time" in cols


def test_routing_log_fanout_fields_nullable():
    """parent_log_id 和 plan_step_index 当前为 NULL，保留扩展位。"""
    from app.modules.ai.models.routing_log import AiRoutingLog

    cols = AiRoutingLog.__table__.columns
    assert cols["parent_log_id"].nullable is True
    assert cols["plan_step_index"].nullable is True


def test_routing_log_no_rule_hits_column():
    """LLM-only 路由日志不应包含 rule_hits 列。"""
    from app.modules.ai.models.routing_log import AiRoutingLog

    assert "rule_hits" not in AiRoutingLog.__table__.columns
