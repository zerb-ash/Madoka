from __future__ import annotations

import re
from typing import Any, Pattern

_TRAP_PHRASES: tuple[str, ...] = (
    "buy this=ban",
    "catching limited snipers",
    "catch item snipers",
    "do not buy this",
    "you will get banned",
    "this is to catch item snipers",
)

_BLOCK_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(r"do\s*not\s*buy", re.I),
    re.compile(r"don'?t\s*buy", re.I),
    re.compile(r"do\s*not\s*purchase", re.I),
    re.compile(r"item\s*snipe", re.I),
    re.compile(r"you\s*will\s*get\s*banned", re.I),
    re.compile(r"if\s*you\s*buy.*banned", re.I),
    re.compile(r"get\s*banned", re.I),
    re.compile(r"will\s*be\s*banned", re.I),
    re.compile(r"buy\s*this\s*=\s*ban", re.I),
    re.compile(r"catch(?:ing)?\s*limited\s*snipers?", re.I),
)


def _item_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    desc = str(item.get("description") or "")
    return f"{name}\n{desc}"


def trap_reason(item: dict[str, Any]) -> str | None:
    text = _item_text(item)
    if not text.strip():
        return None
    lowered = text.lower()
    for phrase in _TRAP_PHRASES:
        if phrase in lowered:
            return phrase
    for pat in _BLOCK_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def is_trap_item(item: dict[str, Any]) -> bool:
    return trap_reason(item) is not None


def name_has_keyword(name: str, keyword: str) -> bool:
    key = keyword.strip().lower()
    if not key:
        return False
    return key in name.lower()


def item_matches_keywords(item: dict[str, Any], keywords: list[str]) -> str | None:
    name = str(item.get("name") or "")
    if not name:
        return None
    for keyword in keywords:
        if name_has_keyword(name, keyword):
            return keyword
    return None
