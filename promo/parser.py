from __future__ import annotations

import re
from dataclasses import dataclass

ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
PROMO_LABEL_RE = re.compile(r"promocode\s*:\s*([A-Za-z0-9]+)", re.I)
ALLCAPS_TOKEN_RE = re.compile(r"\b([A-Z0-9]{2,})\b")
# After a code ping, also accept mixed alnum tokens (e.g. VoteForVancy2026).
MIXED_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{2,})\b")

MIXED_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{2,})\b")


@dataclass(frozen=True, slots=True)
class PromoHit:
    codes: tuple[str, ...]
    has_ping: bool
    channel_id: int
    message_id: int
    content: str
    is_test: bool


def has_promo_ping(content: str, *, role_id: int | None) -> bool:
    text = content or ""
    mentioned = ROLE_MENTION_RE.findall(text)
    if role_id is not None:
        if str(role_id) in mentioned:
            return True
        return f"<@&{role_id}>" in text
    # No role configured — treat any role mention / "code ping(s)" text as a ping.
    lowered = text.lower()
    if "code ping" in lowered or "code pings" in lowered:
        return True
    return bool(mentioned)


def extract_promo_codes(content: str, *, allow_mixed: bool = False) -> list[str]:
    text = content or ""
    found: list[str] = []

    for m in PROMO_LABEL_RE.finditer(text):
        code = re.sub(r"[^A-Za-z0-9]", "", m.group(1)).upper()
        if _ok(code):
            found.append(code)

    cleaned = ROLE_MENTION_RE.sub(" ", text)
    cleaned = re.sub(r"https?://\S+", " ", cleaned, flags=re.I)

    for m in ALLCAPS_TOKEN_RE.finditer(cleaned):
        code = m.group(1).upper()
        if _ok(code):
            found.append(code)

    if allow_mixed:
        for m in MIXED_TOKEN_RE.finditer(cleaned):
            raw = m.group(1)
            if raw.isupper():
                continue
            code = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
            if _ok(code):
                found.append(code)

    # de-dupe preserve order
    return list(dict.fromkeys(found))


def _ok(code: str) -> bool:
    if len(code) < 2:
        return False
    if code.isdigit():
        return False
    if not any(ch.isalpha() for ch in code):
        return False
    return True


def parse_promo_message(
    *,
    content: str,
    channel_id: int,
    message_id: int,
    role_id: int | None = None,
    is_test: bool = False,
) -> PromoHit:
    has_ping = has_promo_ping(content, role_id=role_id)
    codes = extract_promo_codes(content, allow_mixed=has_ping)
    return PromoHit(
        codes=tuple(codes),
        has_ping=has_ping,
        channel_id=int(channel_id),
        message_id=int(message_id),
        content=(content or "")[:500],
        is_test=bool(is_test),
    )
