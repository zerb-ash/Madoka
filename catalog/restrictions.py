from __future__ import annotations

from typing import Any


def restrictions_flags(item: dict[str, Any]) -> tuple[bool, bool]:
    raw = item.get("itemRestrictions")
    if not isinstance(raw, list):
        return False, False
    limited_u = "LimitedUnique" in raw
    limited = "Limited" in raw or limited_u
    return limited, limited_u


def normalize_item(item: dict[str, Any], stub: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(item)
    limited, limited_u = restrictions_flags(out)
    if out.get("itemRestrictions") is not None:
        out["isLimited"] = limited
        out["isLimitedUnique"] = limited_u
    elif stub:
        if "isLimitedUnique" in stub and "isLimitedUnique" not in out:
            out["isLimitedUnique"] = stub["isLimitedUnique"]
        if "isLimited" in stub and "isLimited" not in out:
            out["isLimited"] = stub["isLimited"]
    return out


def is_limited(item: dict[str, Any]) -> bool:
    if item.get("isLimited") or item.get("isLimitedUnique"):
        return True
    limited, limited_u = restrictions_flags(item)
    return limited or limited_u


def is_limited_unique(item: dict[str, Any]) -> bool:
    if item.get("isLimitedUnique"):
        return True
    _limited, limited_u = restrictions_flags(item)
    return limited_u
