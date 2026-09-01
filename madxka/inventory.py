from __future__ import annotations

from typing import Any


def parse_inventory_row(row: dict[str, Any]) -> dict[str, Any]:
    item = row.get("Item") if isinstance(row.get("Item"), dict) else {}
    product = row.get("Product") if isinstance(row.get("Product"), dict) else {}
    icon = row.get("AssetRestrictionIcon") if isinstance(row.get("AssetRestrictionIcon"), dict) else {}
    return {
        "asset_id": int(item.get("AssetId") or 0),
        "name": str(item.get("Name") or ""),
        "asset_type": int(item.get("AssetType") or 0),
        "price_in_robux": product.get("PriceInRobux"),
        "serial_number": product.get("SerialNumber"),
        "restriction_css": str(icon.get("CssTag") or ""),
        "raw": row,
    }


def parse_inventory_payload(payload: Any) -> tuple[list[dict[str, Any]], str | None, int]:
    if not isinstance(payload, dict):
        return [], None, 0
    data = payload.get("Data")
    if not isinstance(data, dict):
        return [], None, 0
    rows = data.get("Items")
    items = [parse_inventory_row(r) for r in rows] if isinstance(rows, list) else []
    cursor = data.get("nextPageCursor")
    total = int(data.get("TotalItems") or len(items))
    next_cursor = str(cursor) if cursor not in (None, "", "null") else None
    return items, next_cursor, total
