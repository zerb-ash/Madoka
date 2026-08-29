from __future__ import annotations

import re

SITE = "https://madxka.com"
API = f"{SITE}/apisite"

_slug_ws = re.compile(r"\s+")
_slug_dash = re.compile(r"\s+-\s+")
_slug_multi = re.compile(r"-+")


def catalog_slug(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return "item"
    slug = _slug_dash.sub("-", raw)
    slug = _slug_ws.sub("-", slug)
    slug = _slug_multi.sub("-", slug).strip("-")
    return slug or "item"


def catalog_item(item_id: int, name: str | None = None) -> str:
    item_id = int(item_id)
    slug = catalog_slug(name) if name else str(item_id)
    return f"{SITE}/catalog/{item_id}/{slug}"


def user_profile(user_id: int) -> str:
    return f"{SITE}/users/{int(user_id)}/profile"


def group_page(group_id: int) -> str:
    return f"{SITE}/groups/{int(group_id)}/group"


def thumb_url(path: str) -> str:
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        return f"{SITE}{path}"
    return f"{SITE}/{path}"
