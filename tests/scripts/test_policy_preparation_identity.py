"""Preparation credentials stay local to the stopped installation session."""

from types import SimpleNamespace

import pytest

from app.core.exceptions import BusinessException
from tools.ops.policy_preparation_identity import PreparationSessions


def test_session_tokens_are_process_local_expiring_and_revocable(monkeypatch):
    sessions = PreparationSessions()
    identity = SimpleNamespace(tenant="identity")
    token = sessions.issue(identity)
    assert "." not in token
    assert sessions.resolve(token) is identity
    sessions.revoke(token)
    with pytest.raises(BusinessException):
        sessions.resolve(token)
    token = sessions.issue(identity)
    monkeypatch.setattr(
        "tools.ops.policy_preparation_identity.time.monotonic", lambda: 10**12
    )
    with pytest.raises(BusinessException):
        sessions.resolve(token)


def test_preparation_rate_and_session_bounds():
    sessions = PreparationSessions()
    for _ in range(10):
        sessions.attempt()
    with pytest.raises(BusinessException) as error:
        sessions.attempt()
    assert error.value.code == 429
    for _ in range(16):
        sessions.issue(object())
    with pytest.raises(BusinessException) as error:
        sessions.issue(object())
    assert error.value.code == 429


def test_clear_invalidates_all_sessions():
    sessions = PreparationSessions()
    token = sessions.issue(object())
    sessions.clear()
    with pytest.raises(BusinessException):
        sessions.resolve(token)
