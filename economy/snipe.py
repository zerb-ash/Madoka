from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from catalog.filter import is_trap_item, trap_reason
from catalog.item_kind import can_be_limited
from catalog.restrictions import is_limited, is_limited_unique
from economy.guard import is_purchase_blocked
from economy.service import CURRENCY_ROBUX, EconomyService

LIMITED_WATCH_SECONDS = 10
LIMITED_POLL_INTERVAL = 0.5  # 120/min details checks while watching a drop
# Back-compat alias used by embeds / client messages
LIMITED_RECHECK_SECONDS = LIMITED_WATCH_SECONDS


def limited_check_max() -> int:
    return max(1, int(round(LIMITED_WATCH_SECONDS / LIMITED_POLL_INTERVAL)))


def _serial_count(item: dict[str, Any]) -> int:
    serials = item.get("serialCount")
    if serials is None:
        return 0
    return int(serials)


def _sale_count(item: dict[str, Any]) -> int:
    sales = item.get("saleCount")
    if sales is None:
        return 0
    return int(sales)


def _serial_supply(item: dict[str, Any]) -> int:
    serials = _serial_count(item)
    if serials > 0:
        return serials
    sales = _sale_count(item)
    if sales > 0:
        return sales
    return 1


def remaining_serials(item: dict[str, Any]) -> int | None:
    """Copies still available: serialCount - saleCount (None if no serial pool)."""
    serials = item.get("serialCount")
    if serials is None:
        return None
    total = int(serials)
    if total <= 0:
        return 0
    return max(0, total - _sale_count(item))


def limited_kind_label(item: dict[str, Any]) -> str:
    limited = is_limited(item)
    limited_u = is_limited_unique(item)
    if limited and limited_u:
        # LimitedUnique implies Limited in flags; call out both when Unique is set
        raw = item.get("itemRestrictions")
        has_plain = isinstance(raw, list) and "Limited" in raw
        has_unique = isinstance(raw, list) and "LimitedUnique" in raw
        if has_plain and has_unique:
            return "Limited + Limited U"
        if has_unique:
            return "Limited U"
        return "Limited + Limited U"
    if limited_u:
        return "Limited U"
    if limited:
        return "Limited"
    return "none"


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


def channel_snipe_delay_seconds(item: dict[str, Any]) -> float:
    """Human-like delay after both catalog + channel flags are true."""
    supply = _serial_supply(item)
    if supply <= 50:
        return random.uniform(1.0, 1.5)
    return random.uniform(2.0, 5.0)


# Avoid serials 1-3: wait until saleCount >= 3 so next copy is #4+.
SAFEBUY_MIN_SALES = 3
SAFEBUY_POLL_INTERVAL = 0.25
SAFEBUY_URGENT_LEFT = 5
SAFEBUY_URGENT_RATIO = 0.85
# Extra grace after human delay if sales still < min (don't hang forever).
SAFEBUY_GRACE_SECONDS = 2.0


def _safebuy_urgent(item: dict[str, Any]) -> bool:
    """True when stock is burning fast enough that waiting risks missing the drop."""
    supply = _serial_supply(item)
    sales = _sale_count(item)
    left = remaining_serials(item)
    if left is not None and left <= SAFEBUY_URGENT_LEFT:
        return True
    if supply > 0 and sales / supply >= SAFEBUY_URGENT_RATIO:
        return True
    return False


async def await_channel_buy_window(
    item: dict[str, Any],
    *,
    refresh_item: Callable[[int], Awaitable[dict[str, Any] | None]],
    delay: float,
    safebuy: bool = False,
    on_tick: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], str]:
    """
    Wait the channel delay, optionally polling sales (SAFEBUY).

    Returns (item, reason) where reason is why buy was allowed.
    """
    item_id = int(item.get("id") or 0)
    delay = max(0.0, float(delay))

    if not safebuy:
        if delay > 0:
            await asyncio.sleep(delay)
            if item_id:
                fresh = await refresh_item(item_id)
                if fresh:
                    item = fresh
        return item, "delay"

    started = time.monotonic()
    deadline = started + delay
    hard_deadline = deadline + SAFEBUY_GRACE_SECONDS
    reason = "safebuy-timeout"

    while True:
        sales = _sale_count(item)
        supply = _serial_supply(item)
        left = remaining_serials(item)
        urgent = _safebuy_urgent(item)
        now = time.monotonic()
        past_delay = now >= deadline
        sales_ok = sales >= SAFEBUY_MIN_SALES

        # Prefer: human delay done + ≥3 sales (next serial is #4+).
        if past_delay and sales_ok:
            reason = f"safebuy-ok sales={sales}>={SAFEBUY_MIN_SALES}"
            break
        # Stock rushing — buy even if still under min sales / mid-delay.
        if urgent:
            reason = (
                f"safebuy-urgent sales={sales} left={left} "
                f"supply={supply} (avoid miss)"
            )
            break
        if now >= hard_deadline:
            reason = (
                f"safebuy-timeout sales={sales}<{SAFEBUY_MIN_SALES} "
                f"left={left} (buy anyway)"
            )
            break

        status = (
            f"safebuy wait · sales={sales}/{SAFEBUY_MIN_SALES} · "
            f"left={left} · supply={supply} · "
            f"{'post-delay' if past_delay else f'delay {max(0.0, deadline - now):.2f}s left'}"
        )
        print(f"[safebuy] `{item_id}` {status}")
        if on_tick is not None:
            try:
                await on_tick(item, status)
            except Exception:
                pass

        sleep_for = min(SAFEBUY_POLL_INTERVAL, max(0.05, hard_deadline - now))
        await asyncio.sleep(sleep_for)
        if item_id:
            try:
                fresh = await refresh_item(item_id)
            except Exception as e:
                print(f"[safebuy] refresh failed `{item_id}`: {e}")
                fresh = None
            if fresh:
                item = fresh

    sales = _sale_count(item)
    left = remaining_serials(item)
    print(
        f"[safebuy] `{item_id}` buy gate · {reason} · "
        f"sales={sales} serials={_serial_count(item)} left={left} "
        f"elapsed={time.monotonic() - started:.2f}s"
    )
    return item, reason


def robux_list_price(item: dict[str, Any]) -> int | None:
    if item.get("isForSale") is False:
        return None
    price = item.get("price")
    if price is None:
        return None
    return int(price)


def can_auto_snipe(item: dict[str, Any], *, balance: int, allow_timed: bool = False) -> str | None:
    if not is_limited(item):
        return "not limited"
    if is_trap_item(item) or is_purchase_blocked(item):
        return f"blocked ({trap_reason(item) or 'trap'})"
    if is_egg_item(item):
        return "egg"
    if not allow_timed and is_timed_limited(item):
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
    allow_timed: bool = False,
    skip_serial_delay: bool = False,
) -> dict[str, Any]:
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    label = f"`{item_id}` {name}"

    if not is_limited(item):
        return {"ok": False, "reason": "not limited", "item_id": item_id}

    if skip_serial_delay:
        delay = 0.0
        supply = _serial_supply(item)
        print(f"[auto-snipe] queued {label} · supply {supply} · delay 0.00s (channel-gated)")
    else:
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
    block = can_auto_snipe(item, balance=robux, allow_timed=allow_timed)
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
    buy: bool = True,
) -> dict[str, Any]:
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    label = f"`{item_id}` {name}"

    if not can_be_limited(item):
        print(f"[auto-snipe] skip recheck {label} · asset type not limited-capable")
        return {"ok": False, "reason": "asset type not limited-capable", "item_id": item_id}

    if is_limited(item):
        left = remaining_serials(item)
        print(
            f"[auto-snipe] already limited {label} · kind={limited_kind_label(item)} · "
            f"sales={_sale_count(item)} serials={_serial_count(item)} left={left}"
        )
        if not buy:
            if on_limited is not None:
                await on_limited(item, 0)
            return {"ok": True, "purchased": False, "item_id": item_id, "limited": True}
        return await auto_snipe_limited(economy, item=item, refresh_item=refresh_item)

    checks_max = limited_check_max()
    print(
        f"[auto-snipe] waiting for limited {label} · "
        f"{LIMITED_WATCH_SECONDS}s @ {LIMITED_POLL_INTERVAL}s ({checks_max} checks)"
    )
    for attempt in range(1, checks_max + 1):
        await asyncio.sleep(LIMITED_POLL_INTERVAL)
        try:
            fresh = await refresh_item(item_id)
        except Exception as e:
            print(f"[auto-snipe] recheck {attempt}/{checks_max} failed {label}: {e}")
            if on_check is not None:
                try:
                    await on_check(attempt, item)
                except Exception as cb_err:
                    print(f"[auto-snipe] check callback failed {label}: {cb_err}")
            continue
        if fresh:
            item = fresh
        limited = is_limited(item)
        left = remaining_serials(item)
        print(
            f"[auto-snipe] recheck {attempt}/{checks_max} {label} · "
            f"kind={limited_kind_label(item)} · "
            f"sales={_sale_count(item)} serials={_serial_count(item)} left={left}"
        )
        if on_check is not None:
            try:
                await on_check(attempt, item)
            except Exception as e:
                print(f"[auto-snipe] check callback failed {label}: {e}")
        if not limited:
            continue
        if buy:
            result = await auto_snipe_limited(economy, item=item, refresh_item=refresh_item)
        else:
            result = {"ok": True, "purchased": False, "item_id": item_id, "limited": True}
        if on_limited is not None:
            try:
                await on_limited(item, attempt)
            except Exception as e:
                print(f"[auto-snipe] limited callback failed {label}: {e}")
        result["limited_checks"] = attempt
        return result

    print(f"[auto-snipe] not limited after {LIMITED_WATCH_SECONDS}s {label}")
    return {
        "ok": False,
        "reason": "not limited",
        "item_id": item_id,
        "limited_checks": checks_max,
    }
