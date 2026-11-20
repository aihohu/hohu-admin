"""Closed loopback preparation routes; no grant, IO or normal-admission change."""

from app.core.field_ingress import _binding, _reject
from app.core.field_ingress_manifest import INGRESS_BINDINGS

_MANAGEMENT = frozenset(
    row[:4] for row in INGRESS_BINDINGS if row[4] == "field-management"
)
_LOCAL = frozenset(
    (
        (method,),
        path,
        "APIRoute",
        "tools.ops.policy_prepare.create_app.<locals>." + name,
    )
    for method, path, name in (
        ("POST", "/policy-prepare/login", "login"),
        ("POST", "/policy-prepare/logout", "logout"),
        ("GET", "/policy-prepare/catalog", "catalog"),
        ("POST", "/policy-prepare/command", "command"),
        ("POST", "/policy-prepare/finish", "finish"),
    )
)
_ASSETS = frozenset(
    (
        ("GET",),
        path,
        "APIRoute",
        "tools.ops.policy_preparation_assets.PreparationAssets.mount.<locals>.asset",
    )
    for path in ("/", "/{asset_path:path}")
)


def validate_preparation_ingress(application):
    """Check the complete preparation app immediately before HTTP serving.

    The existing controller has already obtained maintenance evidence and may
    have prepared the catalog. This check neither replaces nor reopens it.
    Asset-byte/manifest verification remains with PreparationAssets.
    """
    if len(application.routes) > 64:
        _reject(limit=True)
    seen = set()
    methods = 0
    for route in application.routes:
        key = _binding(route)
        if key not in _MANAGEMENT | _LOCAL | _ASSETS or key in seen:
            _reject()
        seen.add(key)
        methods += len(key[0]) or 1
        if methods > 128:
            _reject(limit=True)
    if seen != _MANAGEMENT | _LOCAL and seen != _MANAGEMENT | _LOCAL | _ASSETS:
        _reject()
