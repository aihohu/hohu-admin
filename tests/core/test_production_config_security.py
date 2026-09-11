"""Production settings must reject unsafe public deployment defaults."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

DATABASE_URL = "postgresql+asyncpg://user:password@postgres:5432/hohu"
STRONG_SECRET = "a-unique-production-secret-with-at-least-32-characters"


def _production_settings(**overrides) -> Settings:
    values = {
        "ENV": "prod",
        "DATABASE_URL": DATABASE_URL,
        "SECRET_KEY": STRONG_SECRET,
        "SERVER_URL": "",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "secret",
    [
        "short",
        "change_me_in_production",
        "<YOUR_SECRET_KEY>",
    ],
)
def test_production_rejects_weak_or_public_secret(secret: str) -> None:
    with pytest.raises(ValidationError):
        _production_settings(SECRET_KEY=secret)


@pytest.mark.parametrize(
    "server_url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/base-path",
        "//example.com",
    ],
)
def test_production_rejects_unsafe_or_non_origin_server_url(server_url: str) -> None:
    with pytest.raises(ValidationError):
        _production_settings(SERVER_URL=server_url)


@pytest.mark.parametrize(
    "server_url", ["", "https://example.com", "https://example.com/"]
)
def test_production_accepts_same_origin_or_https_origin(server_url: str) -> None:
    configured = _production_settings(SERVER_URL=server_url)

    assert configured.SERVER_URL == server_url.rstrip("/")


def test_jwt_algorithm_is_fixed_to_hs256() -> None:
    with pytest.raises(ValidationError):
        _production_settings(ALGORITHM="none")
