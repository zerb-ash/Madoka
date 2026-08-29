from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from catalog.service import CatalogDiff


@dataclass(slots=True)
class PollWindow:
    polls: int = 0
    added: dict[int, dict[str, Any]] = field(default_factory=dict)
    changed: dict[int, dict[str, Any]] = field(default_factory=dict)
    removed: dict[int, dict[str, Any]] = field(default_factory=dict)

    def reset(self) -> None:
        self.polls = 0
        self.added.clear()
        self.changed.clear()
        self.removed.clear()

    def merge(self, diff: CatalogDiff) -> None:
        for stub in diff.added:
            item_id = int(stub.get("id") or 0)
            if not item_id:
                continue
            self.added[item_id] = stub
            self.changed.pop(item_id, None)
            self.removed.pop(item_id, None)

        for _old, stub in diff.changed:
            item_id = int(stub.get("id") or 0)
            if not item_id:
                continue
            if item_id not in self.added:
                self.changed[item_id] = stub
            self.removed.pop(item_id, None)

        for stub in diff.removed:
            item_id = int(stub.get("id") or 0)
            if not item_id:
                continue
            self.removed[item_id] = stub
            self.added.pop(item_id, None)
            self.changed.pop(item_id, None)
