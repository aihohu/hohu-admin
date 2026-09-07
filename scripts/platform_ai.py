"""Audited CLI client for the dedicated platform AI control plane."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_TOKEN_ENV = "HOHU_PLATFORM_ACCESS_TOKEN"
_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_TENANT_ID_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_MAX_PAYLOAD_BYTES = 64 * 1024


def _positive_id(value: str) -> str:
    if _ID_RE.fullmatch(value) is None or int(value) > 9_223_372_036_854_775_807:
        raise argparse.ArgumentTypeError("ID must be a positive signed-bigint string")
    return value


def _tenant_id(value: str) -> str:
    if _TENANT_ID_RE.fullmatch(value) is None or int(value) > 9_223_372_036_854_775_807:
        raise argparse.ArgumentTypeError("tenant ID must be a signed-bigint string")
    return value


def _positive_number(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _page_size(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("page size must be between 1 and 100")
    return parsed


def _payload_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--payload-file",
        required=True,
        type=Path,
        help="UTF-8 JSON object file; secrets must never be passed in argv.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call the audited HoHu platform AI control plane."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    resources = parser.add_subparsers(dest="resource", required=True)

    agents = resources.add_parser("agents")
    agent_actions = agents.add_subparsers(dest="action", required=True)
    agent_actions.add_parser("list")
    agent_get = agent_actions.add_parser("get")
    agent_get.add_argument("--agent-id", required=True, type=_positive_id)
    agent_update = agent_actions.add_parser("update")
    agent_update.add_argument("--agent-id", required=True, type=_positive_id)
    _payload_path(agent_update)

    providers = resources.add_parser("providers")
    provider_actions = providers.add_subparsers(dest="action", required=True)
    provider_list = provider_actions.add_parser("list")
    provider_list.add_argument("--current", type=_positive_number, default=1)
    provider_list.add_argument("--size", type=_page_size, default=20)
    provider_create = provider_actions.add_parser("create")
    _payload_path(provider_create)
    for action in ("update", "delete", "test"):
        command = provider_actions.add_parser(action)
        command.add_argument("--provider-id", required=True, type=_positive_id)
        if action == "update":
            _payload_path(command)
        if action == "test":
            command.add_argument("--model-id", required=True, type=_positive_id)

    models = resources.add_parser("models")
    model_actions = models.add_subparsers(dest="action", required=True)
    for action in ("list", "create", "update", "delete"):
        command = model_actions.add_parser(action)
        command.add_argument("--provider-id", required=True, type=_positive_id)
        if action in {"update", "delete"}:
            command.add_argument("--model-id", required=True, type=_positive_id)
        if action in {"create", "update"}:
            _payload_path(command)

    policies = resources.add_parser("policies")
    policy_actions = policies.add_subparsers(dest="action", required=True)
    for action in ("list", "set", "delete"):
        command = policy_actions.add_parser(action)
        command.add_argument("--tenant-id", required=True, type=_tenant_id)
        if action in {"set", "delete"}:
            command.add_argument("--model-id", required=True, type=_positive_id)
        if action == "set":
            _payload_path(command)
    return parser


def parser_help() -> str:
    return _build_parser().format_help()


def parse_arguments() -> argparse.Namespace:
    return _build_parser().parse_args()


def _read_payload(path: Path) -> dict[str, Any]:
    if path.stat().st_size > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload file exceeds 64 KiB")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload file must contain one JSON object")
    return payload


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an http(s) origin without credentials")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True, repr=False)
class PlatformApiRequest:
    base_url: str
    method: str
    path: str
    headers: dict[str, str]
    params: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            "PlatformApiRequest("
            f"base_url={self.base_url!r}, method={self.method!r}, path={self.path!r})"
        )


def build_request(arguments: argparse.Namespace) -> PlatformApiRequest:
    token = os.getenv(_TOKEN_ENV, "")
    if not token:
        raise ValueError(f"{_TOKEN_ENV} is required")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Platform-Reason": arguments.reason,
        "X-Platform-Ticket": arguments.ticket_id,
        "X-Correlation-ID": arguments.correlation_id,
    }
    method = "GET"
    params = None
    payload = None
    if arguments.resource == "agents":
        path = "/platform/ai/agents"
        if arguments.action == "get":
            path += f"/{arguments.agent_id}"
        elif arguments.action == "update":
            path += f"/{arguments.agent_id}"
            method = "PUT"
            payload = _read_payload(arguments.payload_file)
    elif arguments.resource == "providers":
        path = "/platform/ai/providers"
        if arguments.action == "list":
            params = {"current": arguments.current, "size": arguments.size}
        elif arguments.action == "create":
            method = "POST"
            payload = _read_payload(arguments.payload_file)
        elif arguments.action == "update":
            path += f"/{arguments.provider_id}"
            method = "PUT"
            payload = _read_payload(arguments.payload_file)
        elif arguments.action == "delete":
            path += f"/{arguments.provider_id}"
            method = "DELETE"
        else:
            path += f"/{arguments.provider_id}/test"
            method = "POST"
            payload = {"modelId": arguments.model_id}
    elif arguments.resource == "models":
        path = f"/platform/ai/providers/{arguments.provider_id}/models"
        if arguments.action == "create":
            method = "POST"
            payload = _read_payload(arguments.payload_file)
        elif arguments.action in {"update", "delete"}:
            path += f"/{arguments.model_id}"
            method = "PUT" if arguments.action == "update" else "DELETE"
            if arguments.action == "update":
                payload = _read_payload(arguments.payload_file)
    else:
        path = f"/platform/tenants/{arguments.tenant_id}/ai/model-policies"
        if arguments.action in {"set", "delete"}:
            path += f"/{arguments.model_id}"
            method = "PUT" if arguments.action == "set" else "DELETE"
            if arguments.action == "set":
                payload = _read_payload(arguments.payload_file)
    return PlatformApiRequest(
        base_url=_validate_base_url(arguments.base_url),
        method=method,
        path=path,
        headers=headers,
        params=params,
        payload=payload,
    )


def execute(request: PlatformApiRequest) -> dict[str, Any]:
    with httpx.Client(
        base_url=request.base_url,
        headers=request.headers,
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = client.request(
            request.method,
            request.path,
            params=request.params,
            json=request.payload,
        )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 200:
        raise ValueError("platform API returned a non-success envelope")
    return body


def main() -> None:
    try:
        result = execute(build_request(parse_arguments()))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise SystemExit(f"Platform AI request failed: {type(exc).__name__}") from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
