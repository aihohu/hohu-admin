"""Explicit browser origin for the normal console during a fenced handover."""

import re
from urllib.parse import urlsplit


def console_authority(origin):
    if not isinstance(origin, str) or len(origin) > 320:
        raise ValueError("Invalid admin console origin")
    parsed = urlsplit(origin)
    host = parsed.hostname
    if (
        not host
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
        or ".." in host
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and host not in {"localhost", "127.0.0.1"})
    ):
        raise ValueError("Invalid admin console origin")
    port = parsed.port
    authority = host if port is None else f"{host}:{port}"
    if origin != f"{parsed.scheme}://{authority}" or port == 0:
        raise ValueError("Use the exact admin console origin without a path")
    return authority
