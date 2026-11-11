"""Bounded JSON objects for trusted, closed-schema contract validators."""

import json
import math

from app.core.exceptions import InvalidParameterException


def _invalid():
    return InvalidParameterException(
        "Invalid closed JSON input", error_code="CLOSED_JSON_INVALID"
    )


def _limit():
    return InvalidParameterException(
        "Input exceeds the contract budget",
        error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def closed_object(value, *, max_bytes, max_depth=8, max_nodes=1024):
    """Return a detached object and canonical bytes; never an authorization proof."""
    if any(type(v) is not int or v < 1 for v in (max_bytes, max_depth, max_nodes)):
        raise _invalid()

    def utf8(text):
        if len(text) > max_bytes:
            raise _limit()
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            raise _invalid() from None
        if len(encoded) > max_bytes:
            raise _limit()
        return encoded

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise _invalid()
            result[key] = item
        return result

    def constant(_value):
        raise _invalid()

    if type(value) is str:
        utf8(value)
        try:
            value = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, RecursionError):
            raise _invalid() from None
    if type(value) is not dict:
        raise _invalid()
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise _limit()
        kind = type(item)
        if kind is dict:
            if len(item) > max_nodes:
                raise _limit()
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise _invalid()
                visit(key, depth + 1)
                result[key] = visit(child, depth + 1)
            return result
        if kind is list:
            if len(item) > max_nodes:
                raise _limit()
            return [visit(child, depth + 1) for child in item]
        if kind is str:
            utf8(item)
        elif kind is int:
            if item.bit_length() > 64:
                raise _invalid()
        elif kind is float:
            if not math.isfinite(item):
                raise _invalid()
        elif kind not in (bool, type(None)):
            raise _invalid()
        return item

    detached = visit(value)
    try:
        encoded = json.dumps(
            detached,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, UnicodeError):
        raise _invalid() from None
    if len(encoded) > max_bytes:
        raise _limit()
    return detached, encoded
