from __future__ import annotations

import re
from typing import Any, Pattern

_BLOCK_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(r"do\s*not\s*buy", re.I),
    re.compile(r"don'?t\s*buy", re.I),
    re.compile(r"do\s*not\s*purchase", re.I),
    re.compile(r"item\s*snipe", re.I),
    re.compile(r"you\s*will\s*get\s*banned", re.I),
    re.compile(r"if\s*you\s*buy.*banned", re.I),
    re.compile(r"get\s*banned", re.I),
    re.compile(r"will\s*be\s*banned", re.I),
)


def _item_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    desc = str(item.get("description") or "")
    return f"{name}\n{desc}"


def purchase_block_reason(item: dict[str, Any]) -> str | None:
    text = _item_text(item)
    if not text.strip():
        return None
    for pat in _BLOCK_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def is_purchase_blocked(item: dict[str, Any]) -> bool:
    return purchase_block_reason(item) is not None
