from __future__ import annotations

import asyncio
import random
from typing import Any

from catalog.filter import is_trap_item, trap_reason
from catalog.restrictions import is_limited
from economy.guard import is_purchase_blocked
from economy.service import CURRENCY_ROBUX, EconomyService


def _serial_supply(item: dict[str, Any]) -> int:
    serials = item.get("serialCount")
    if serials is not None and int(serials) > 0:
        return int(serials)
    sales = item.get("saleCount")
    if sales is not None and int(sales) > 0:
        return int(sales)
    return 1


def snipe_delay_seconds(item: dict[str, Any]) -> float:
    supply = _serial_supply(item)
    if supply <= 50:
        return 0.0
    if supply <= 150:
        return random.uniform(0.2, 3.0)
    return random.uniform(3.0, 5.0)


def robux_list_price(item: dict[str, Any]) -> int | None:
    if item.get("isForSale") is False:
        return None
    price = item.get("price")
    if price is None:
        return None
    return int(price)


def can_auto_snipe(item: dict[str, Any], *, balance: int) -> str | None:
    if not is_limited(item):
        return "not limited"
    if is_trap_item(item) or is_purchase_blocked(item):
        return f"blocked ({trap_reason(item) or 'trap'})"
    price = robux_list_price(item)
    if price is None:
        return "no robux listing"
    if item.get("priceTickets") is not None and item.get("price") is None:
        return "tix only"
    if balance < price:
        return f"insufficient balance ({balance:,} < {price:,})"
    return None


async def auto_snipe_limited(
    economy: EconomyService,
    *,
    item: dict[str, Any],
    refresh_item,
) -> dict[str, Any]:
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    label = f"`{item_id}` {name}"

    if not is_limited(item):
        return {"ok": False, "reason": "not limited", "item_id": item_id}

    delay = snipe_delay_seconds(item)
    supply = _serial_supply(item)
    print(f"[auto-snipe] queued {label} · supply {supply} · delay {delay:.2f}s")

    if delay > 0:
        await asyncio.sleep(delay)
        fresh = await refresh_item(item_id)
        if fresh:
            item = fresh

    try:
        bal = await economy.balance()
    except Exception as e:
        print(f"[auto-snipe] balance failed {label}: {e}")
        return {"ok": False, "reason": f"balance failed: {e}", "item_id": item_id}

    robux = int(bal.get("robux") or 0)
    block = can_auto_snipe(item, balance=robux)
    if block:
        print(f"[auto-snipe] skip {label} · {block}")
        return {"ok": False, "reason": block, "item_id": item_id}

    price = robux_list_price(item)
    if price is None:
        print(f"[auto-snipe] skip {label} · no robux price")
        return {"ok": False, "reason": "no robux price", "item_id": item_id}

    user = bal.get("user") or {}
    uid = int(user.get("id") or 0)
    if uid:
        try:
            if await economy.owns_item(uid, item):
                print(f"[auto-snipe] skip {label} · already owned")
                return {"ok": False, "reason": "already owned", "item_id": item_id}
        except Exception as e:
            print(f"[auto-snipe] own-check failed {label}: {e}")

    print(f"[auto-snipe] buying {label} · {price:,} R$ · balance {robux:,}")
    try:
        outcome = await economy.purchase(item, currency=CURRENCY_ROBUX)
    except Exception as e:
        print(f"[auto-snipe] failed {label}: {e}")
        return {"ok": False, "reason": str(e), "item_id": item_id}

    if outcome.get("purchased"):
        print(f"[auto-snipe] bought {label} · {price:,} R$")
        return {"ok": True, "purchased": True, "item_id": item_id, "price": price, "outcome": outcome}

    reason = str(outcome.get("reason") or "declined")
    print(f"[auto-snipe] declined {label} · {reason}")
    return {"ok": False, "reason": reason, "item_id": item_id, "outcome": outcome}
