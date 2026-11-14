"""Old free-form titles cannot silently acquire authored provenance."""

from types import SimpleNamespace

from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.service.conversation_delivery import safe_title


def test_title_origin_is_persistent_closed_and_unknown_by_default():
    table = AiConversation.__table__
    column = table.c.title_origin
    assert str(column.server_default.arg) == "legacy_unknown"
    assert not column.nullable
    assert any(
        "title_origin" in str(getattr(c, "sqltext", ""))
        and "authored" in str(c.sqltext)
        for c in table.constraints
    )


def test_unknown_history_title_is_hidden_and_current_authored_title_is_projectable():

    assert (
        safe_title(
            SimpleNamespace(title="HISTORIC-SECRET", title_origin="legacy_unknown")
        )
        is None
    )
    assert (
        safe_title(SimpleNamespace(title="My title", title_origin="authored"))
        == "My title"
    )
    assert safe_title(SimpleNamespace(title="新对话", title_origin="fixed")) == "新对话"
    assert safe_title(SimpleNamespace(title="forged", title_origin="fixed")) is None
