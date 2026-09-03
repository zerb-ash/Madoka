from __future__ import annotations

from typing import Any

CLOTHING_TYPES = frozenset({2, 11, 12})
LIMITED_ASSET_TYPES = frozenset({8, 18, 19, 41, 42, 43, 44, 45, 46, 47})


def is_clothing(item: dict[str, Any]) -> bool:
    at = item.get("assetType")
    if at is None:
        return False
    return int(at) in CLOTHING_TYPES


def can_be_limited(item: dict[str, Any]) -> bool:
    at = item.get("assetType")
    if at is None:
        return False
    return int(at) in LIMITED_ASSET_TYPES
