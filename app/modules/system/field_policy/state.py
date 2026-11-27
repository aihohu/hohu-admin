"""Closed durable state invariants shared by barriers and active consumers."""

import re


def valid_policy_state(mode, ever_enforced, candidate, active):
    def digest(value):
        return (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    if candidate is not None and not digest(candidate):
        return False
    if mode in {"legacy", "ready"}:
        return (
            ever_enforced is False
            and active is None
            and (mode == "legacy" or candidate is not None)
        )
    return (
        mode in {"enforcing", "frozen"}
        and ever_enforced is True
        and digest(active)
        and (mode == "frozen" or candidate == active)
    )
