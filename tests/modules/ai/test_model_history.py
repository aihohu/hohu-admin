"""Only reauthorized server history and durable receipts enter a new run."""

from unittest.mock import AsyncMock

from app.modules.ai.schemas.message import MessageOut, MessageTombstoneOut
from app.modules.ai.service.chat_service import chat_service


async def test_history_omits_tombstones_and_carries_durable_tool_receipts(monkeypatch):
    visible = MessageOut.model_construct(
        message_id=11,
        role="assistant",
        content="",
        parts=None,
        tool_calls=[
            {
                "tool": "dept.move",
                "ok": True,
                "result": {"moved": 1},
                "ui": {"downloadUrl": "private-ui-only"},
                "args": {"secret": "private-args"},
            }
        ],
    )
    hidden = MessageTombstoneOut(messageId=12, role="assistant")
    monkeypatch.setattr(
        chat_service, "load_history", AsyncMock(return_value=[visible, hidden])
    )

    messages, dependencies = await chat_service.load_model_history(None, 1, None)

    assert dependencies == [11]
    assert len(messages) == 1
    text = messages[0]["parts"][0]["text"]
    assert '"moved": 1' in text
    assert '"ok": true' in text
    assert "private-ui-only" not in text
    assert "private-args" not in text


async def test_history_preserves_attachment_references_without_sending_client_history(
    monkeypatch,
):
    original = [{"type": "file", "fileId": "123", "mediaType": "text/csv", "url": ""}]
    monkeypatch.setattr(
        chat_service,
        "load_history",
        AsyncMock(
            return_value=[
                MessageOut.model_construct(
                    message_id=10,
                    role="user",
                    content="导入它",
                    parts=original,
                    tool_calls=None,
                )
            ]
        ),
    )
    messages, dependencies = await chat_service.load_model_history(None, 1, None)
    assert dependencies == []
    assert messages[0]["parts"][-1]["fileId"] == "123"
    assert original == [
        {"type": "file", "fileId": "123", "mediaType": "text/csv", "url": ""}
    ]


async def test_hidden_turn_does_not_replay_its_old_user_command(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "load_history",
        AsyncMock(
            return_value=[
                MessageOut.model_construct(
                    message_id=1, role="user", content="修改其他人的资料", parts=None
                ),
                MessageTombstoneOut(messageId=2, role="assistant"),
                MessageOut.model_construct(
                    message_id=3, role="user", content="只查当前人数", parts=None
                ),
                MessageOut.model_construct(
                    message_id=4,
                    role="assistant",
                    content="当前 4 人",
                    parts=None,
                    tool_calls=None,
                ),
            ]
        ),
    )
    messages, dependencies = await chat_service.load_model_history(None, 1, None)
    assert [message["id"] for message in messages] == ["3", "4"]
    assert dependencies == [4]
