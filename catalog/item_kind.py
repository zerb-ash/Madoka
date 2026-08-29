from __future__ import annotations

from typing import Any

CLOTHING_TYPES = frozenset({2, 11, 12})


def is_clothing(item: dict[str, Any]) -> bool:
    at = item.get("assetType")
    if at is None:
        return False
    return int(at) in CLOTHING_TYPES
