from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CATALOG_ID_RE = re.compile(
    r"(?:https?://)?(?:www\.)?madxka\.com/catalog/(\d+)",
    re.I,
)
# Test posts can be just an id /catalog/123 or bare number line
BARE_CATALOG_PATH_RE = re.compile(r"(?:^|[\s/])catalog/(\d+)", re.I)
BARE_ID_RE = re.compile(r"(?m)^\s*(\d{3,8})\s*$")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


@dataclass(frozen=True, slots=True)
class DropMessage:
    item_ids: tuple[int, ...]
    has_ping: bool
    has_catalog_link: bool
    channel_id: int
    message_id: int
    content: str
    is_drop: bool
    is_test: bool


def _message_blob(content: str, embeds: list[Any] | None = None) -> str:
    parts = [content or ""]
    for emb in embeds or []:
        if not isinstance(emb, dict):
            continue
        for key in ("title", "description", "url"):
            val = emb.get(key)
            if val:
                parts.append(str(val))
        for field in emb.get("fields") or []:
            if not isinstance(field, dict):
                continue
            if field.get("name"):
                parts.append(str(field["name"]))
            if field.get("value"):
                parts.append(str(field["value"]))
    return "\n".join(parts)


def parse_drop_message(
    *,
    content: str,
    embeds: list[Any] | None = None,
    channel_id: int,
    message_id: int,
    role_id: int | None = None,
    is_test: bool = False,
) -> DropMessage:
    blob = _message_blob(content, embeds)
    found: list[int] = [int(m) for m in CATALOG_ID_RE.findall(blob)]
    has_full_link = len(found) > 0

    if is_test:
        for m in BARE_CATALOG_PATH_RE.findall(blob):
            found.append(int(m))
        for m in BARE_ID_RE.findall(content or ""):
            found.append(int(m))

    ids = tuple(dict.fromkeys(found))
    has_link = len(ids) > 0

    has_ping = False
    if role_id is not None:
        has_ping = str(role_id) in ROLE_MENTION_RE.findall(content or "")
        if not has_ping:
            lowered = (content or "").lower()
            has_ping = "item drops ping" in lowered or f"<@&{role_id}>" in (content or "")
    else:
        has_ping = "item drops ping" in (content or "").lower() or bool(
            ROLE_MENTION_RE.search(content or "")
        )

    # Drop confirmation = catalog id/link present. Ping is never required,
    # especially in the test channel.
    is_drop = has_link
    return DropMessage(
        item_ids=ids,
        has_ping=has_ping,
        has_catalog_link=has_full_link or has_link,
        channel_id=int(channel_id),
        message_id=int(message_id),
        content=blob[:500],
        is_drop=is_drop,
        is_test=bool(is_test),
    )
