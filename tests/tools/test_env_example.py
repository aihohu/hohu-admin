"""Deployment template values must survive the runtime dotenv parser."""

from pathlib import Path

from dotenv import dotenv_values


def test_example_redis_password_is_empty():
    template = Path(__file__).resolve().parents[2] / ".env.example"
    values = dotenv_values(template, interpolate=False)
    assert values["REDIS_PASSWORD"] == ""
