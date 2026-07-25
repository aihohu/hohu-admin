"""spec §7.1c: ai_routing_feedback 表 schema 验证."""

# ruff: noqa: PLC0415


def test_routing_feedback_table_exists():
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    assert AiRoutingFeedback.__tablename__ == "ai_routing_feedback"


def test_routing_feedback_columns():
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    cols = AiRoutingFeedback.__table__.columns
    for name in (
        "feedback_id",
        "message_id",
        "user_id",
        "original_agent",
        "feedback",
        "corrected_agent",
        "trace_id",
        "create_time",
    ):
        assert name in cols, f"missing column {name}"


def test_routing_feedback_check_constraints():
    """spec §7.1c: 2 个 CHECK 约束 — feedback 枚举 + correction 必填匹配."""
    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    constraint_names = {
        c.name for c in AiRoutingFeedback.__table__.constraints if c.name
    }
    assert "ck_ai_routing_feedback_type" in constraint_names
    assert "ck_ai_routing_feedback_correction_match" in constraint_names
