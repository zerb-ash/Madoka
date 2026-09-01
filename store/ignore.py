from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from catalog.filter import name_has_keyword
from store.cache import _atomic_write


class IgnoreStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.keywords: list[str] = []
        self.load()

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.is_file():
            self.keywords = []
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            self.keywords = [str(k).strip().lower() for k in raw if str(k).strip()]
            return
        if isinstance(raw, dict):
            items = raw.get("keywords")
            if isinstance(items, list):
                self.keywords = [str(k).strip().lower() for k in items if str(k).strip()]
                return
        self.keywords = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.path,
            json.dumps({"keywords": self.keywords}, indent=2, ensure_ascii=False),
        )

    def list_keywords(self) -> list[str]:
        return list(self.keywords)

    def add(self, keyword: str) -> tuple[bool, str]:
        key = keyword.strip().lower()
        if not key:
            return False, "empty"
        if key in self.keywords:
            return False, "duplicate"
        self.keywords.append(key)
        self.keywords.sort()
        self.save()
        return True, key

    def remove(self, keyword: str) -> tuple[bool, str]:
        key = keyword.strip().lower()
        if key not in self.keywords:
            return False, "missing"
        self.keywords.remove(key)
        self.save()
        return True, key

    def matches(self, item: dict[str, Any]) -> str | None:
        name = str(item.get("name") or "")
        if not name:
            return None
        for keyword in self.keywords:
            if name_has_keyword(name, keyword):
                return keyword
        return None
