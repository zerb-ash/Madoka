from __future__ import annotations

from typing import Any

from catalog.filter import is_trap_item, trap_reason


def purchase_block_reason(item: dict[str, Any]) -> str | None:
    return trap_reason(item)


def is_purchase_blocked(item: dict[str, Any]) -> bool:
    return is_trap_item(item)
