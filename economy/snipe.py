from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from catalog.filter import is_trap_item, trap_reason
from catalog.item_kind import can_be_limited
from catalog.restrictions import is_limited, is_limited_unique
from economy.guard import is_purchase_blocked
from economy.service import CURRENCY_ROBUX, EconomyService

LIMITED_RECHECK_SECONDS = 10


def _serial_count(item: dict[str, Any]) -> int:
    serials = item.get("serialCount")
    if serials is None:
        return 0
    return int(serials)


def _serial_supply(item: dict[str, Any]) -> int:
    serials = _serial_count(item)
    if serials > 0:
        return serials
    sales = item.get("saleCount")
    if sales is not None and int(sales) > 0:
        return int(sales)
    return 1


def is_timed_limited(item: dict[str, Any]) -> bool:
    return is_limited(item) and _serial_count(item) == 0


def is_egg_item(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").lower()
    desc = str(item.get("description") or "").lower()
    return "egg" in name or "egg" in desc


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
    if is_egg_item(item):
        return "egg"
    if is_timed_limited(item):
        return "timed limited (serial 0)"
    price = robux_list_price(item)
    if price is None:
        return "no robux listing"
    if item.get("priceTickets") is not None and item.get("price") is None:
        return "tix only"
    if balance < price:
        return f"insufficient balance ({balance:,} < {price:,})"
    return None


def _serial_from_result(result: dict[str, Any]) -> Any:
    for key in ("serialNumber", "SerialNumber", "serial", "ownedSerialNumber"):
        if result.get(key) is not None:
            return result.get(key)
    return None


async def _owned_serial(economy: EconomyService, item: dict[str, Any], uid: int) -> Any:
    asset_id = int(item.get("id") or 0)
    asset_type = item.get("assetType")
    if not asset_id or asset_type is None:
        return None
    try:
        rows = await economy.http.inventory_all(uid, int(asset_type))
    except Exception:
        return None
    for row in rows:
        if int(row.get("asset_id") or 0) != asset_id:
            continue
        serial = row.get("serial_number")
        if serial is not None:
            return serial
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
        result = outcome.get("result") or {}
        serial = _serial_from_result(result if isinstance(result, dict) else {})
        if serial is None and uid:
            serial = await _owned_serial(economy, item, uid)
        print(f"[auto-snipe] bought {label} · {price:,} R$ · serial {serial}")
        return {
            "ok": True,
            "purchased": True,
            "item_id": item_id,
            "name": name,
            "price": price,
            "serial": serial,
            "outcome": outcome,
        }

    reason = str(outcome.get("reason") or "declined")
    print(f"[auto-snipe] declined {label} · {reason}")
    return {"ok": False, "reason": reason, "item_id": item_id, "outcome": outcome}


async def watch_limited_then_snipe(
    economy: EconomyService,
    *,
    item: dict[str, Any],
    refresh_item: Callable[[int], Awaitable[dict[str, Any] | None]],
    on_check: Callable[[int, dict[str, Any]], Awaitable[None]] | None = None,
    on_limited: Callable[[dict[str, Any], int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    label = f"`{item_id}` {name}"

    if not can_be_limited(item):
        print(f"[auto-snipe] skip recheck {label} · asset type not limited-capable")
        return {"ok": False, "reason": "asset type not limited-capable", "item_id": item_id}

    if is_limited(item):
        return await auto_snipe_limited(economy, item=item, refresh_item=refresh_item)

    print(f"[auto-snipe] waiting for limited {label} · {LIMITED_RECHECK_SECONDS}s")
    for attempt in range(1, LIMITED_RECHECK_SECONDS + 1):
        await asyncio.sleep(1)
        try:
            fresh = await refresh_item(item_id)
        except Exception as e:
            print(f"[auto-snipe] recheck {attempt}/{LIMITED_RECHECK_SECONDS} failed {label}: {e}")
            if on_check is not None:
                try:
                    await on_check(attempt, item)
                except Exception as cb_err:
                    print(f"[auto-snipe] check callback failed {label}: {cb_err}")
            continue
        if fresh:
            item = fresh
        limited = is_limited(item)
        print(
            f"[auto-snipe] recheck {attempt}/{LIMITED_RECHECK_SECONDS} {label} · "
            f"limited={'yes' if limited else 'no'} · "
            f"limited_u={'yes' if is_limited_unique(item) else 'no'}"
        )
        if on_check is not None:
            try:
                await on_check(attempt, item)
            except Exception as e:
                print(f"[auto-snipe] check callback failed {label}: {e}")
        if not limited:
            continue
        result = await auto_snipe_limited(economy, item=item, refresh_item=refresh_item)
        if on_limited is not None:
            try:
                await on_limited(item, attempt)
            except Exception as e:
                print(f"[auto-snipe] limited callback failed {label}: {e}")
        result["limited_checks"] = attempt
        return result

    print(f"[auto-snipe] not limited after {LIMITED_RECHECK_SECONDS}s {label}")
    return {
        "ok": False,
        "reason": "not limited",
        "item_id": item_id,
        "limited_checks": LIMITED_RECHECK_SECONDS,
    }
