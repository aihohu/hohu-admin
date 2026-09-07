"""Platform AI CLI is a fixed-route, audited API client."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import scripts.platform_ai as cli


def test_cli_reads_token_from_environment_and_builds_fixed_policy_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOHU_PLATFORM_ACCESS_TOKEN", "platform-secret-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform_ai.py",
            "--base-url",
            "https://control.example.test",
            "--reason",
            "Rotate tenant model",
            "--ticket-id",
            "OPS-501",
            "--correlation-id",
            "plan5bc-cli-1",
            "policies",
            "list",
            "--tenant-id",
            "9001",
        ],
    )

    request = cli.build_request(cli.parse_arguments())

    assert request.method == "GET"
    assert request.path == "/platform/tenants/9001/ai/model-policies"
    assert request.headers["Authorization"] == "Bearer platform-secret-token"
    assert "platform-secret-token" not in repr(request)


def test_cli_rejects_missing_environment_token(monkeypatch) -> None:
    monkeypatch.delenv("HOHU_PLATFORM_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform_ai.py",
            "--base-url",
            "https://control.example.test",
            "--reason",
            "Inspect agents",
            "--ticket-id",
            "OPS-502",
            "--correlation-id",
            "plan5bc-cli-2",
            "agents",
            "list",
        ],
    )

    with pytest.raises(ValueError, match="HOHU_PLATFORM_ACCESS_TOKEN"):
        cli.build_request(cli.parse_arguments())


def test_cli_does_not_offer_arbitrary_path_or_api_key_arguments(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["platform_ai.py", "--help"])
    with pytest.raises(SystemExit):
        cli.parse_arguments()
    help_text = cli.parser_help()
    assert "--path" not in help_text
    assert "--api-key" not in help_text
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "app.db" not in source


def test_cli_rejects_base_url_paths_before_sending_platform_token() -> None:
    with pytest.raises(ValueError, match="origin"):
        cli._validate_base_url("https://control.example.test/untrusted-base")
