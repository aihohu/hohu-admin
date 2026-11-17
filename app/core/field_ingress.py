"""Fail closed on undeclared HTTP/static registrations, before external IO.

This inventory supplies no identity, field grant or legacy fallback. The actual
owners still prove current authorization and source closure for every request.
"""

import inspect
import os
from types import MappingProxyType

from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_ingress_manifest import INGRESS_BINDINGS, INGRESS_CONTRACTS

_KINDS = MappingProxyType({APIRoute: "APIRoute", Route: "Route", Mount: "Mount"})
_DECLARATIONS = frozenset(row[:4] for row in INGRESS_BINDINGS)


def _reject(*, limit=False):
    raise BusinessException(
        500,
        "字段授权入口超出预算" if limit else "字段授权入口未登记",
        error_code="FIELD_INGRESS_LIMIT" if limit else "FIELD_INGRESS_UNDECLARED",
    )


def _binding(route):
    kind = _KINDS.get(type(route))
    if kind is None:
        _reject()
    path = getattr(route, "path", None)
    if type(path) is not str or not path.startswith("/") or len(path) > 256:
        _reject(limit=True)
    if kind == "Mount":
        target = type(route.app)
        reference = target.__module__ + "." + target.__qualname__
        callback = getattr(route.app, "is_public", None)
        directory = getattr(route.app, "directory", None)
        if (
            not inspect.isfunction(callback)
            or callback.__module__ + "." + callback.__qualname__
            != "app.main.is_public_upload"
            or type(directory) is not str
            or os.path.normcase(os.path.abspath(directory))
            != os.path.normcase(os.path.abspath(settings.UPLOAD_DIR))
            or getattr(route.app, "html", True)
            or getattr(route.app, "follow_symlink", True)
            or getattr(route.app, "packages", None)
        ):
            _reject()
        methods = ()
    else:
        target = inspect.unwrap(route.endpoint)
        if not inspect.isfunction(target):
            _reject()
        reference = target.__module__ + "." + target.__qualname__
        methods = tuple(sorted(route.methods or ()))
        if not methods or any(type(m) is not str or len(m) > 12 for m in methods):
            _reject()
    if len(reference) > 512:
        _reject(limit=True)
    return methods, path, kind, reference


def field_ingress_inventory(application, *, ai_enabled):
    """Return a detached code inventory; never inspect a request or business row."""
    if type(ai_enabled) is not bool:
        _reject()
    if len(application.routes) > 512:
        _reject(limit=True)
    mode = "on" if ai_enabled else "off"
    declared = {
        row[:4]: row[4] for row in INGRESS_BINDINGS if row[5] in {"always", mode}
    }
    seen = set()
    seen_pairs = set()
    method_count = 0
    output = []
    for route in application.routes:
        key = _binding(route)
        if key not in _DECLARATIONS or key not in declared or key in seen:
            _reject()
        seen.add(key)
        methods, path, kind, reference = key
        # A second handler for the same method/path must not shadow the first.
        pairs = {(path, method) for method in methods or ("MOUNT",)}
        if pairs & seen_pairs:
            _reject()
        seen_pairs.update(pairs)
        method_count += len(pairs)
        if method_count > 2048:
            _reject(limit=True)
        contract = declared[key]
        category, domain, owner, source = INGRESS_CONTRACTS[contract]
        output.append(
            {
                "methods": methods,
                "path": path,
                "kind": kind,
                "binding": reference,
                "contract": contract,
                "category": category,
                "domain": domain,
                "owner": owner,
                "source": source,
            }
        )
    return tuple(output)


def validate_field_ingress(application, *, ai_enabled):
    field_ingress_inventory(application, ai_enabled=ai_enabled)
