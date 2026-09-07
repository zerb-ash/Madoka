from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from catalog.filter import is_trap_item
from catalog.item_kind import can_be_limited
from catalog.restrictions import is_limited
from drop.parser import DropMessage
from economy.snipe import (
    _serial_supply,
    auto_snipe_limited,
    await_channel_buy_window,
    channel_snipe_delay_seconds,
)

OnNotify = Callable[[str], Awaitable[None]]
RefreshItem = Callable[[int], Awaitable[dict[str, Any] | None]]
BuyItem = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class FlagState:
    catalog_ready: bool = False
    channel_ready: bool = False
    item: dict[str, Any] | None = None
    channel_message_id: int | None = None
    updated_at: float = field(default_factory=time.time)
    sniping: bool = False
    sniped: bool = False


class DualFlagCoordinator:
    def __init__(
        self,
        *,
        economy,
        refresh_item: RefreshItem,
        notify: OnNotify,
        flag_wait_seconds: int = 120,
        delay_ms_min: int = 50,
        delay_ms_max: int = 200,
        safebuy: bool = False,
        debug: bool = False,
    ) -> None:
        self.economy = economy
        self.refresh_item = refresh_item
        self.notify = notify
        self.flag_wait_seconds = max(5, int(flag_wait_seconds))
        # legacy unused ms knobs kept for settings compat; channel delay uses serials
        self.delay_ms_min = max(0, int(delay_ms_min))
        self.delay_ms_max = max(self.delay_ms_min, int(delay_ms_max))
        self.safebuy = bool(safebuy)
        self.debug = debug
        self._flags: dict[int, FlagState] = {}
        self._lock = asyncio.Lock()
        self._safebuy_notified: set[int] = set()

    def _log(self, msg: str) -> None:
        print(f"[dual-flag] {msg}")

    def _get(self, item_id: int) -> FlagState:
        state = self._flags.get(item_id)
        if state is None:
            state = FlagState()
            self._flags[item_id] = state
        return state

    def debug_flags(self, item_id: int) -> str:
        state = self._flags.get(item_id)
        if state is None:
            return f"id={item_id} catalog=False channel=False"
        return (
            f"id={item_id} catalog={state.catalog_ready} channel={state.channel_ready} "
            f"sniping={state.sniping} sniped={state.sniped}"
        )

    async def on_catalog_limited(self, item: dict[str, Any]) -> None:
        item_id = int(item.get("id") or 0)
        if not item_id:
            return
        if is_trap_item(item):
            self._log(f"catalog skip `{item_id}` trap")
            return
        if not can_be_limited(item) and not is_limited(item):
            self._log(f"catalog skip `{item_id}` not limited-capable")
            return

        async with self._lock:
            state = self._get(item_id)
            state.catalog_ready = True
            state.item = item
            state.updated_at = time.time()
            both = state.catalog_ready and state.channel_ready
            self._log(self.debug_flags(item_id))

        name = str(item.get("name") or item_id)
        await self.notify(
            f"Catalog flag ready · `{item_id}` **{name}** · waiting for channel drop"
            + (" · channel already ready" if both else "")
        )
        if both:
            await self._try_snipe(item_id, source="catalog+channel")

    async def on_channel_drop(self, drop: DropMessage) -> None:
        if not drop.is_drop or not drop.item_ids:
            self._log(
                f"ignore non-drop msg {drop.message_id} · "
                f"link={drop.has_catalog_link} ping={drop.has_ping}"
            )
            return

        for item_id in drop.item_ids:
            await self._handle_channel_item(item_id, drop)

    async def _handle_channel_item(self, item_id: int, drop: DropMessage) -> None:
        async with self._lock:
            state = self._get(item_id)
            state.channel_ready = True
            state.channel_message_id = drop.message_id
            state.updated_at = time.time()
            catalog_ready = state.catalog_ready
            self._log(self.debug_flags(item_id))

        await self.notify(
            f"Channel flag ready · `{item_id}` · msg `{drop.message_id}` · "
            f"test={'yes' if drop.is_test else 'no'} · "
            f"ping={'yes' if drop.has_ping else 'no (not required)'} · "
            f"catalog={'yes' if catalog_ready else 'no'} · "
            f"`{self.debug_flags(item_id)}`"
        )

        if catalog_ready:
            await self._try_snipe(item_id, source="channel+catalog")
            return

        # Channel-first: inspect now
        item = await self.refresh_item(item_id)
        if not item:
            self._log(f"channel-first `{item_id}` missing from catalog")
            await self.notify(f"Channel drop `{item_id}` not in catalog yet")
            return

        async with self._lock:
            state = self._get(item_id)
            state.item = item

        if not is_limited(item):
            self._log(f"channel-first `{item_id}` not limited yet")
            await self.notify(f"Channel drop `{item_id}` not limited yet · skipping auto-buy")
            return

        async with self._lock:
            state = self._get(item_id)
            state.catalog_ready = True
            self._log(self.debug_flags(item_id))

        await self.notify(f"Channel-first limited confirmed · `{item_id}` · both flags true")
        await self._try_snipe(item_id, source="channel-first")

    async def _try_snipe(self, item_id: int, *, source: str) -> None:
        async with self._lock:
            state = self._get(item_id)
            self._log(
                f"snipe attempt source={source} · {self.debug_flags(item_id)}"
            )
            if state.sniped or state.sniping:
                self._log(f"skip `{item_id}` already sniping/sniped")
                return
            if not (state.catalog_ready and state.channel_ready):
                self._log(f"skip `{item_id}` flags incomplete")
                return
            state.sniping = True
            item = state.item

        if item is None:
            item = await self.refresh_item(item_id)
        else:
            fresh = await self.refresh_item(item_id)
            if fresh:
                item = fresh

        if item is None:
            async with self._lock:
                self._get(item_id).sniping = False
            await self.notify(f"Snipe aborted `{item_id}` · details missing")
            return

        supply = _serial_supply(item)
        delay = channel_snipe_delay_seconds(item)
        mode = "safebuy" if self.safebuy else "delay"
        self._log(
            f"both flags true `{item_id}` · supply={supply} · "
            f"{mode} {delay:.2f}s · source={source}"
        )
        await self.notify(
            f"Both flags true · `{item_id}` **{item.get('name')}** · "
            f"supply `{supply}` · "
            + (
                f"**SAFEBUY** window **{delay:.2f}s** · wait ≥3 sales (skip #1-3) · source `{source}`"
                if self.safebuy
                else f"waiting **{delay:.2f}s** then buy · source `{source}`"
            )
        )

        async def _tick(_fresh: dict[str, Any], status: str) -> None:
            # Avoid spamming watch channel every 0.25s — log only once mid-wait.
            if item_id in self._safebuy_notified:
                return
            self._safebuy_notified.add(item_id)
            await self.notify(f"`[safebuy]` `{item_id}` · {status}")

        item, gate = await await_channel_buy_window(
            item,
            refresh_item=self.refresh_item,
            delay=delay,
            safebuy=self.safebuy,
            on_tick=_tick if self.safebuy else None,
        )
        async with self._lock:
            self._get(item_id).item = item
        self._safebuy_notified.discard(item_id)
        if self.safebuy:
            self._log(f"safebuy gate `{item_id}` · {gate}")
            await self.notify(f"`[safebuy]` `{item_id}` · buying · {gate}")

        # Allow timed limiteds for channel-gated sales.
        # Delay / safebuy already applied above — don't add a second instant buy.
        result = await auto_snipe_limited(
            self.economy,
            item=item,
            refresh_item=self.refresh_item,
            allow_timed=True,
            skip_serial_delay=True,
        )

        async with self._lock:
            state = self._get(item_id)
            state.sniping = False
            if result.get("purchased"):
                state.sniped = True

        if result.get("purchased"):
            price = result.get("price")
            serial = result.get("serial")
            name = str(result.get("name") or item.get("name") or item_id)
            await self.notify(
                f"**{name}** has been sniped for **{int(price):,} R$** got serial **{serial if serial is not None else '—'}**"
                if price is not None
                else f"**{name}** has been sniped got serial **{serial if serial is not None else '—'}**"
            )
        else:
            await self.notify(
                f"Snipe failed `{item_id}` · {result.get('reason') or 'declined'}"
            )

    def prune(self) -> None:
        now = time.time()
        dead = [
            item_id
            for item_id, state in self._flags.items()
            if now - state.updated_at > self.flag_wait_seconds and not state.sniping
        ]
        for item_id in dead:
            self._flags.pop(item_id, None)
            if self.debug:
                self._log(f"pruned stale flags `{item_id}`")
