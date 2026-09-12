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

# Poll for robux listing after limited flag (price often lags serials / Limited U).
PRICE_WATCH_SECONDS = 15
PRICE_POLL_INTERVAL = 0.25


def limited_check_max() -> int:
    return max(1, int(round(LIMITED_WATCH_SECONDS / LIMITED_POLL_INTERVAL)))


def price_check_max() -> int:
    return max(1, int(round(PRICE_WATCH_SECONDS / PRICE_POLL_INTERVAL)))


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


# Skip serials #1-5: wait until saleCount >= 5 so next copy is #6+.
SAFEBUY_MIN_SALES = 5
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


async def await_skip_early_serials(
    item: dict[str, Any],
    *,
    refresh_item: Callable[[int], Awaitable[dict[str, Any] | None]],
    on_tick: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
    min_sales: int = SAFEBUY_MIN_SALES,
) -> tuple[dict[str, Any], str]:
    """Poll every 0.25s and buy only after enough sales to skip early serials."""
    item_id = int(item.get("id") or 0)
    started = time.monotonic()
    min_sales = max(1, int(min_sales))

    while True:
        sales = _sale_count(item)
        serials = _serial_count(item)
        left = remaining_serials(item)
        supply = _serial_supply(item)

        if sales >= min_sales:
            reason = f"sales={sales}>={min_sales} (skip #{1}-{min_sales})"
            break
        # Almost gone — buy even if still in the early serials.
        if left is not None and left <= SAFEBUY_URGENT_LEFT:
            reason = f"urgent left={left} sales={sales} (avoid miss)"
            break
        if supply > 0 and sales / supply >= SAFEBUY_URGENT_RATIO:
            reason = f"urgent sales={sales}/{supply} left={left} (avoid miss)"
            break

        status = (
            f"sales={sales}/{min_sales} · serials={serials} · "
            f"left={left} · supply={supply}"
        )
        print(f"[safebuy] `{item_id}` wait · {status}")
        if on_tick is not None:
            try:
                await on_tick(item, status)
            except Exception:
                pass

        await asyncio.sleep(SAFEBUY_POLL_INTERVAL)
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


async def await_robux_list_price(
    item: dict[str, Any],
    refresh_item: Callable[[int], Awaitable[dict[str, Any] | None]],
    *,
    on_check: Callable[[int, dict[str, Any], int | None], Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], int | None]:
    """
    Wait until a robux listing appears (isForSale + price), similar to limited checks.
    Limited flags often arrive before the item is actually put on sale.
    """
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    label = f"`{item_id}` {name}"

    price = robux_list_price(item)
    if price is not None:
        return item, price

    checks_max = price_check_max()
    print(
        f"[auto-snipe] waiting for robux price {label} · "
        f"{PRICE_WATCH_SECONDS}s @ {PRICE_POLL_INTERVAL}s ({checks_max} checks)"
    )

    for attempt in range(1, checks_max + 1):
        if attempt > 1:
            await asyncio.sleep(PRICE_POLL_INTERVAL)
        try:
            fresh = await refresh_item(item_id)
        except Exception as e:
            print(f"[auto-snipe] price check {attempt}/{checks_max} failed {label}: {e}")
            if on_check is not None:
                try:
                    await on_check(attempt, item, None)
                except Exception:
                    pass
            continue
        if fresh:
            item = fresh

        price = robux_list_price(item)
        left = remaining_serials(item)
        for_sale = item.get("isForSale")
        raw_price = item.get("price")
        print(
            f"[auto-snipe] price check {attempt}/{checks_max} {label} · "
            f"forSale={for_sale} price={raw_price} left={left}"
        )
        if on_check is not None:
            try:
                await on_check(attempt, item, price)
            except Exception as e:
                print(f"[auto-snipe] price check callback failed {label}: {e}")

        # Stock gone while waiting for listing.
        if left is not None and left <= 0:
            print(f"[auto-snipe] sold out while waiting for price {label}")
            return item, None
        if price is not None:
            print(f"[auto-snipe] robux price ready {label} · {price:,} R$")
            return item, price

    print(f"[auto-snipe] no robux price after {PRICE_WATCH_SECONDS}s {label}")
    return item, None


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
    on_price_check: Callable[[int, dict[str, Any], int | None], Awaitable[None]] | None = None,
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

    price = robux_list_price(item)
    if price is None:
        item, price = await await_robux_list_price(
            item,
            refresh_item,
            on_check=on_price_check,
        )
    if price is None:
        left = remaining_serials(item)
        if left is not None and left <= 0:
            print(f"[auto-snipe] skip {label} · out of stock")
            return {"ok": False, "reason": "out of stock", "item_id": item_id}
        print(f"[auto-snipe] skip {label} · no robux price")
        return {"ok": False, "reason": "no robux price", "item_id": item_id}

    # Channel-gated path: skip balance/owns round-trips — hit purchase immediately.
    if skip_serial_delay:
        if is_trap_item(item) or is_purchase_blocked(item):
            reason = f"blocked ({trap_reason(item) or 'trap'})"
            print(f"[auto-snipe] skip {label} · {reason}")
            return {"ok": False, "reason": reason, "item_id": item_id}
        if is_egg_item(item):
            print(f"[auto-snipe] skip {label} · egg")
            return {"ok": False, "reason": "egg", "item_id": item_id}
        print(f"[auto-snipe] buying {label} · {price:,} R$ · instant")
        return await _purchase_with_429_retry(
            economy,
            item=item,
            label=label,
            price=price,
            name=name,
            refresh_item=refresh_item,
        )

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
    return await _purchase_with_429_retry(
        economy,
        item=item,
        label=label,
        price=price,
        name=name,
        uid=uid,
        refresh_item=refresh_item,
    )


def _is_429(err: str) -> bool:
    low = (err or "").lower()
    return "429" in low or "toomanyrequests" in low


def _is_out_of_stock(item: dict[str, Any], err: str = "") -> bool:
    left = remaining_serials(item)
    if left is not None and left <= 0:
        return True
    serials = _serial_count(item)
    sales = _sale_count(item)
    if serials > 0 and sales >= serials:
        return True
    # isForSale=false / "not for sale" alone is NOT sold out — listing often lags.
    if left is not None and left > 0:
        return False
    low = (err or "").lower()
    needles = (
        "sold out",
        "out of stock",
        "insufficient quantity",
        "none left",
        "0 remaining",
        "already sold",
    )
    return any(n in low for n in needles)


def _is_listing_not_ready(err: str) -> bool:
    low = (err or "").lower()
    return any(
        n in low
        for n in (
            "not for sale",
            "no longer for sale",
            "price has changed",
            "no robux",
        )
    )


def _is_terminal_purchase_fail(err: str) -> bool:
    low = (err or "").lower()
    needles = (
        "already owned",
        "insufficient funds",
        "insufficient robux",
        "not enough",
        "blocked",
        "floodcheck",
        "moderation",
    )
    return any(n in low for n in needles)


async def _purchase_with_429_retry(
    economy: EconomyService,
    *,
    item: dict[str, Any],
    label: str,
    price: int,
    name: str,
    uid: int = 0,
    refresh_item=None,
) -> dict[str, Any]:
    item_id = int(item.get("id") or 0)
    last_err = ""
    attempt = 0

    while True:
        attempt += 1
        try:
            outcome = await economy.purchase(item, currency=CURRENCY_ROBUX)
        except Exception as e:
            last_err = str(e)
            if _is_out_of_stock(item, last_err):
                print(f"[auto-snipe] out of stock {label} · {last_err}")
                return {"ok": False, "reason": "out of stock", "item_id": item_id}
            if _is_terminal_purchase_fail(last_err):
                print(f"[auto-snipe] failed {label}: {e}")
                return {"ok": False, "reason": last_err, "item_id": item_id}
            if not _is_429(last_err) and not _is_listing_not_ready(last_err):
                if "500" not in last_err and "InternalServerError" not in last_err:
                    print(f"[auto-snipe] failed {label}: {e}")
                    return {"ok": False, "reason": last_err, "item_id": item_id}

            if refresh_item is not None and (
                _is_listing_not_ready(last_err) or "price has changed" in last_err.lower()
            ):
                try:
                    fresh = await refresh_item(item_id)
                    if fresh:
                        item = fresh
                        new_price = robux_list_price(item)
                        if new_price is not None:
                            price = new_price
                        if _is_out_of_stock(item):
                            print(f"[auto-snipe] out of stock {label} · left={remaining_serials(item)}")
                            return {"ok": False, "reason": "out of stock", "item_id": item_id}
                except Exception:
                    pass

            wait = min(1.0, 0.12 + 0.08 * min(attempt, 10))
            kind = "429" if _is_429(last_err) else "listing"
            if attempt == 1 or attempt % 5 == 0:
                print(f"[auto-snipe] {kind} retry #{attempt} {label} · sleep {wait:.2f}s")
            await asyncio.sleep(wait)

            if refresh_item is not None and attempt % 3 == 0:
                try:
                    fresh = await refresh_item(item_id)
                    if fresh:
                        item = fresh
                        new_price = robux_list_price(item)
                        if new_price is not None:
                            price = new_price
                        if _is_out_of_stock(item):
                            print(f"[auto-snipe] out of stock {label} · left={remaining_serials(item)}")
                            return {"ok": False, "reason": "out of stock", "item_id": item_id}
                except Exception:
                    pass
            continue

        if outcome.get("purchased"):
            result = outcome.get("result") or {}
            serial = _serial_from_result(result if isinstance(result, dict) else {})
            if serial is None and uid:
                serial = await _owned_serial(economy, item, uid)
            print(f"[auto-snipe] bought {label} · {price:,} R$ · serial {serial} · tries={attempt}")
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
        if _is_out_of_stock(item, reason):
            print(f"[auto-snipe] out of stock {label} · {reason}")
            return {"ok": False, "reason": "out of stock", "item_id": item_id, "outcome": outcome}
        if _is_429(reason):
            wait = min(1.0, 0.12 + 0.08 * min(attempt, 10))
            if attempt == 1 or attempt % 5 == 0:
                print(f"[auto-snipe] 429 declined retry #{attempt} {label} · sleep {wait:.2f}s")
            await asyncio.sleep(wait)
            continue
        if _is_terminal_purchase_fail(reason):
            print(f"[auto-snipe] declined {label} · {reason}")
            return {"ok": False, "reason": reason, "item_id": item_id, "outcome": outcome}

        # Unknown decline — refresh stock once; stop if gone, else treat as hard fail.
        if refresh_item is not None:
            try:
                fresh = await refresh_item(item_id)
                if fresh:
                    item = fresh
                    if _is_out_of_stock(item):
                        print(f"[auto-snipe] out of stock {label} · left={remaining_serials(item)}")
                        return {"ok": False, "reason": "out of stock", "item_id": item_id}
            except Exception:
                pass
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
