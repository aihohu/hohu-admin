"""Bounded attachment labels, never storage paths or authorization inputs."""

import unicodedata


def attachment_display_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    basename = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    return "".join(
        char for char in basename if not unicodedata.category(char).startswith("C")
    ).strip()[:255]
