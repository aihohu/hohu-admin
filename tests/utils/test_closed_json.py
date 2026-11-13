"""Bound before schema validation; malformed input never echoes its body."""

import pytest

from app.core.exceptions import BusinessException
from app.utils.closed_json import closed_object


def test_snapshot_is_detached_and_deterministic():
    original = {"b": [1, True, None], "a": "汉"}
    parsed, canonical = closed_object(original, max_bytes=8192)
    original["b"].append("later")
    assert parsed == {"a": "汉", "b": [1, True, None]}
    assert canonical == '{"a":"汉","b":[1,true,null]}'.encode()


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
        "[]",
        "null",
        "true",
        '{"x":',
        {1: "value"},
        {"x": object()},
        {"x": (1, 2)},
        '{"x":"\\ud800"}',
        {"x": 2**64},
    ],
)
def test_not_closed_json_is_rejected(raw):
    with pytest.raises(BusinessException):
        closed_object(raw, max_bytes=8192)


def test_depth_node_byte_and_cycle_limits_are_independent():
    with pytest.raises(BusinessException):
        closed_object({"x": "汉" * 2731}, max_bytes=8192)
    deep = {"x": {"x": {"x": {"x": 1}}}}
    with pytest.raises(BusinessException):
        closed_object(deep, max_bytes=8192, max_depth=3)
    with pytest.raises(BusinessException):
        closed_object({"x": [0] * 1024}, max_bytes=8192, max_nodes=1024)
    cycle = {}
    cycle["x"] = cycle
    with pytest.raises(BusinessException):
        closed_object(cycle, max_bytes=8192)
    assert closed_object({"x": "ok"}, max_bytes=10)[0] == {"x": "ok"}


def test_invalid_input_does_not_appear_in_the_domain_error():
    with pytest.raises(BusinessException) as error:
        closed_object('{"sentinel-private": NaN}', max_bytes=8192)
    assert "sentinel-private" not in str(error.value)
