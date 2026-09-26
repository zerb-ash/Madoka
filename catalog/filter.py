from __future__ import annotations

import re
from collections import Counter
from typing import Any, Pattern

_TRAP_PHRASES: tuple[str, ...] = (
    "buy this=ban",
    "buy this = ban",
    "catching limited snipers",
    "catch item snipers",
    "do not buy this",
    "you will get banned",
    "this is to catch item snipers",
    "no buy",
    "item sniper",
    "item snipers",
    "buy and you will be banned",
    "don't buy",
    "dont buy",
    "snipe test",
    "item snipe test",
    "catching snipers",
    "item snipe",
    "sniping items",
)

_BLOCK_PATTERNS: tuple[Pattern[str], ...] = (
    # Word-boundary ban — bare "ban" matched Bandana / Banana / Banded / Urban.
    re.compile(r"\bban(?:ned)?\b", re.I),
    re.compile(r"do\s*not\s*buy", re.I),
    re.compile(r"don'?t\s*buy", re.I),
    re.compile(r"no\s*buy", re.I),
    re.compile(r"do\s*not\s*purchase", re.I),
    re.compile(r"don'?t\s*purchase", re.I),
    re.compile(r"item\s*snipe", re.I),
    re.compile(r"you\s*will\s*get\s*banned", re.I),
    re.compile(r"if\s*you\s*buy.*bann?ed", re.I),
    re.compile(r"get\s*bann?ed", re.I),
    re.compile(r"will\s*be\s*bann?ed", re.I),
    re.compile(r"buy\s*this\s*=\s*ban", re.I),
    re.compile(r"buy\s*=\s*ban", re.I),
    re.compile(r"catch(?:ing)?\s*limited\s*snipers?", re.I),
    re.compile(r"catch(?:ing)?\s*item\s*snipers?", re.I),
)


# Scrambled "DONT BUY" / "BUY BAN" — extra or swapped letters, still a trap.
# "DOPNT BUIY" covers dont+buy with one junk letter each.
_FUZZY_PAIRS: tuple[tuple[str, str], ...] = (
    ("dont", "buy"),
    ("donot", "buy"),
    ("dont", "purchase"),
    ("no", "buy"),
    ("buy", "ban"),
)
# Long smashed forms only. Short "nobuy" is covered by the "no buy" phrase/regex —
# fuzzy-matching it caused false hits on "and you" / "you find".
_FUZZY_BLOBS: tuple[str, ...] = (
    "dontbuy",
    "donotbuy",
    "dontpurchase",
    "buyban",
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _item_text(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    desc = str(item.get("description") or "")
    return f"{name}\n{desc}"


def _norm(text: str) -> str:
    # Drop apostrophes so DN'T / DON'T tokenize as dnt / dont.
    return (text or "").lower().translate(_LEET).replace("'", "").replace("'", "")


def _covers(token: str, target: str, *, max_extra: int, max_miss: int = 0) -> bool:
    if len(token) < len(target) - max_miss or len(token) > len(target) + max_extra:
        return False
    need = Counter(target)
    have = Counter(token)
    matched = sum(min(have[ch], n) for ch, n in need.items())
    missing = len(target) - matched
    extras = len(token) - matched
    return missing <= max_miss and extras <= max_extra


def _fuzzy_trap(text: str) -> str | None:
    norm = _norm(text)
    tokens = [t for t in _TOKEN_RE.findall(norm) if t]
    if not tokens:
        return None

    def hit_word(token: str, target: str) -> bool:
        # "no" may gain one junk letter (NOO); "buy" stays exact to avoid FPs.
        if target == "no":
            return _covers(token, target, max_extra=1, max_miss=0)
        if len(target) <= 2:
            return token == target
        # Allow 1 missing letter on longer stems (DNT≈DONT) and 1 junk letter (DOPNT).
        miss = 1 if len(target) >= 4 else 0
        extra = 1
        if _covers(token, target, max_extra=extra, max_miss=miss):
            return True
        # One token can be two smashed words: DOPNTBUIY
        if len(token) >= len(target) + 3:
            width_max = len(target) + extra
            start_min = len(target) - miss
            for i in range(0, len(token) - start_min + 1):
                for width in range(start_min, min(width_max, len(token) - i) + 1):
                    if _covers(token[i : i + width], target, max_extra=extra, max_miss=miss):
                        return True
        return False

    for i, _token in enumerate(tokens):
        for left, right in _FUZZY_PAIRS:
            # Short stems (no+buy) must be adjacent — avoids "no way buy" FPs.
            span = 2 if min(len(left), len(right)) <= 2 else 3
            window = tokens[i : i + span]
            if len(window) < 2:
                continue
            has_left = any(hit_word(tok, left) for tok in window)
            has_right = any(hit_word(tok, right) for tok in window)
            if has_left and has_right:
                return f"scrambled {left} {right}"

    # Whole-token / smashed-token blobs only — do NOT glue "and"+"you".
    for blob in _FUZZY_BLOBS:
        extra = 2
        for token in tokens:
            if _covers(token, blob, max_extra=extra, max_miss=0):
                return f"scrambled {blob}"
            if len(token) >= len(blob):
                width_max = len(blob) + extra
                for i in range(0, len(token) - len(blob) + 1):
                    for width in range(len(blob), min(width_max, len(token) - i) + 1):
                        if _covers(token[i : i + width], blob, max_extra=extra, max_miss=0):
                            return f"scrambled {blob}"
    return None


def trap_text(text: str) -> str | None:
    raw = text or ""
    if not raw.strip():
        return None
    lowered = _norm(raw)
    for phrase in _TRAP_PHRASES:
        if phrase in lowered:
            return phrase
    for pat in _BLOCK_PATTERNS:
        if pat.search(raw) or pat.search(lowered):
            return pat.pattern
    return _fuzzy_trap(raw)


def trap_reason(item: dict[str, Any]) -> str | None:
    return trap_text(_item_text(item))


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
