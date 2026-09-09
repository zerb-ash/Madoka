from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.auth import can_use_wallet, deny_wallet
from bot.embeds import (
    build_balance_embed,
    build_buy_free_embed,
    build_catalog_stats_embed,
    build_diff_embed,
    build_inspect_pages,
    build_minute_report_embed,
    build_promo_redeem_embed,
)
from bot.inventory_views import InventoryTypeView
from bot.poll_window import PollWindow
from bot.views import InspectPickView, InspectView, send_inspect
from catalog.filter import is_trap_item
from catalog.item_kind import can_be_limited
from catalog.restrictions import is_limited
from catalog.service import CatalogService
from config.settings import Settings
from drop.flags import DualFlagCoordinator
from drop.user_monitor import DropUserMonitor
from economy.service import EconomyService
from economy.snipe import (
    limited_check_max,
    limited_kind_label,
    remaining_serials,
    watch_limited_then_snipe,
)
from madxka.urls import catalog_item
from madxka.http import MadxkaHttp
from promo import PromoSnipeService
from store.cache import CatalogCache
from store.ignore import IgnoreStore


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    return bool(interaction.user.guild_permissions.administrator)


class MadokaBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.cache = CatalogCache(
            catalog_path=settings.catalog_path,
            meta_path=settings.catalog_meta_path,
            details_path=settings.details_path,
        )
        self.api = MadxkaHttp(settings.cookie)
        self.catalog = CatalogService(self.api, self.cache)
        self.economy = EconomyService(self.api)
        self.ignore = IgnoreStore(settings.ignore_path)
        self._watch_channel_cache: discord.abc.Messageable | None = None
        self._poll_window = PollWindow()
        self.dual_flags = DualFlagCoordinator(
            economy=self.economy,
            refresh_item=self._refresh_item,
            notify=self._flag_notify,
            flag_wait_seconds=settings.drop.flag_wait_seconds,
            delay_ms_min=settings.drop.snipe_delay_ms_min,
            delay_ms_max=settings.drop.snipe_delay_ms_max,
            safebuy=settings.safebuy,
            debug=settings.debug,
        )
        self._drop_monitor: DropUserMonitor | None = None
        self._buy_free_task: asyncio.Task[None] | None = None
        self.promo = PromoSnipeService(
            self.api,
            settings.promo,
            notify=self._promo_notify,
            fetch_around=self._promo_fetch_around,
            fetch_recent=self._promo_fetch_recent,
        )

    def _register_commands(self) -> None:
        self.tree.add_command(inspect_cmd)
        self.tree.add_command(balance_cmd)
        self.tree.add_command(buy_group)
        self.tree.add_command(redeem_group)
        self.tree.add_command(catalog_group)
        self.tree.add_command(test_group)
        self.tree.add_command(ignore_group)
        self.tree.add_command(check_group)

    async def _sync_commands(self) -> None:
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

        self._register_commands()

        guild_id = self.settings.discord_guild_id
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        print(f"[madoka] synced {len(synced)} slash command(s)")

    async def setup_hook(self) -> None:
        @self.tree.error
        async def on_tree_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            if isinstance(error, app_commands.CommandNotFound):
                msg = "That command no longer exists. Restart Discord if it still shows up."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                return
            if self.settings.debug:
                print(f"[madoka] command error: {error}")

        print("[madoka] caching catalog...")
        try:
            await self.catalog.warm_start()
            stats = self.catalog.stats()
            print(
                f"[madoka] catalog cached {stats['count']} items · "
                f"hash {str(stats.get('hash') or '?')[:12]}"
            )
        except Exception as e:
            print(f"[madoka] catalog cache failed: {e}")

        await self._sync_commands()

        self._start_drop_monitor()

        self.watch_loop.change_interval(seconds=self.settings.poll_interval_seconds)
        self.watch_loop.start()

    async def on_ready(self) -> None:
        print(f"[madoka] logged in as {self.user}")
        try:
            stats = self.catalog.stats()
            print(f"[madoka] catalog ready · {stats['count']} items cached")
            user = await self.economy.session_user()
            bal = await self.economy.balance()
            print(f"[madoka] session {user.get('name')} · {bal['robux']:,} R$")
        except Exception as e:
            print(f"[madoka] session check failed: {e}")

    async def close(self) -> None:
        if self.watch_loop.is_running():
            self.watch_loop.cancel()
        if self._buy_free_task and not self._buy_free_task.done():
            self._buy_free_task.cancel()
            try:
                await self._buy_free_task
            except asyncio.CancelledError:
                pass
            self._buy_free_task = None
        if self._drop_monitor is not None:
            await self._drop_monitor.stop()
            self._drop_monitor = None
        await self.api.close()
        await super().close()

    async def _watch_channel(self) -> discord.abc.Messageable | None:
        if self._watch_channel_cache is not None:
            return self._watch_channel_cache

        channel_id = self.settings.watch_channel_id
        if not channel_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as e:
                print(f"[madoka] watch channel fetch failed: {e}")
                return None
        if not isinstance(channel, discord.abc.Messageable):
            print(f"[madoka] watch channel {channel_id} is not messageable")
            return None
        self._watch_channel_cache = channel
        return channel

    async def _watch_send(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        channel = await self._watch_channel()
        if channel is None:
            print("[madoka] watch send skipped: no channel")
            return None
        try:
            msg = await channel.send(content=content, embed=embed, view=view)
            if view is not None:
                view.message = msg
            return msg
        except Exception as e:
            print(f"[madoka] watch send failed: {e}")
            self._watch_channel_cache = None
            return None

    async def _resolve_items(self, stubs: list[dict[str, Any]], *, fresh: bool = False) -> list[dict[str, Any]]:
        if not stubs:
            return []
        if fresh:
            rows = await self.catalog.fetch_fresh_stubs(stubs)
        else:
            rows = await self.catalog.hydrate_stubs(stubs)
        by_id = {int(r.get("id") or 0): r for r in rows}
        out: list[dict] = []
        for stub in stubs:
            item_id = int(stub.get("id") or 0)
            row = by_id.get(item_id)
            if row:
                out.append(row)
            elif item_id:
                out.append({"id": item_id, "name": str(item_id)})
        return out

    def _drop_ping(self, item: dict[str, Any]) -> str | None:
        if not is_limited(item):
            return None
        return f"<@{self.settings.wallet_owner_id}>"

    def _skip_drop(self, item: dict[str, Any]) -> str | None:
        if is_trap_item(item):
            return "trap"
        ignored = self.ignore.matches(item)
        if ignored:
            return f"ignore:{ignored}"
        return None

    async def _flag_notify(self, text: str) -> None:
        print(f"[dual-flag] {text}")
        await self._watch_send(content=text)

    async def _promo_notify(self, text: str) -> None:
        await self._watch_send(content=f"`[promo]` {text}")

    async def _promo_fetch_around(self, channel_id: int, message_id: int) -> list[dict[str, Any]]:
        mon = self._drop_monitor
        if mon is None:
            return []
        return await mon.fetch_around(channel_id, message_id, limit=5)

    async def _promo_fetch_recent(self, channel_id: int, limit: int) -> list[dict[str, Any]]:
        mon = self._drop_monitor
        if mon is None:
            return []
        return await mon.fetch_recent(channel_id, limit=limit)

    async def _refresh_item(self, item_id: int) -> dict[str, Any] | None:
        return await self.catalog.lookup_id(item_id, force=True)

    def _start_drop_monitor(self) -> None:
        drop = self.settings.drop
        promo = self.settings.promo
        tokens = [t for t in (drop.user_token, drop.user_token_fallback) if t]
        channels = drop.watched_channel_ids() | promo.watched_channel_ids()
        if not tokens or not channels:
            print("[drop-monitor] disabled (missing token/channel)")
            return

        promo_channels = promo.watched_channel_ids()

        async def on_drop(message) -> None:
            if message.channel_id in promo_channels:
                return
            print(
                f"[drop-monitor] DROP accepted ids={list(message.item_ids)} "
                f"test={message.is_test} ping={message.has_ping} "
                f"flags={self.dual_flags.debug_flags(message.item_ids[0]) if message.item_ids else 'n/a'}"
            )
            await self._watch_send(
                content=(
                    f"`[drop-debug]` channel drop detected · "
                    f"ids=`{', '.join(str(i) for i in message.item_ids)}` · "
                    f"test={'yes' if message.is_test else 'no'} · "
                    f"ping={'yes' if message.has_ping else 'no (not required)'}"
                )
            )
            await self.dual_flags.on_channel_drop(message)

        async def on_raw(raw, message) -> None:
            if message.channel_id in promo_channels:
                try:
                    await self.promo.on_discord_message(raw)
                except Exception as e:
                    print(f"[promo] handler failed: {e}")
                return
            # Always mirror heard messages into watch channel so you can tell it's alive.
            preview = (message.content or "(empty)").replace("\n", " | ")[:140]
            kind = "DROP" if message.is_drop else "heard"
            await self._watch_send(
                content=(
                    f"`[drop-debug]` {kind} · ch=`{message.channel_id}` · "
                    f"msg=`{message.message_id}` · "
                    f"ids=`{', '.join(str(i) for i in message.item_ids) or '—'}` · "
                    f"ping={'yes' if message.has_ping else 'no'} · {preview}"
                )
            )

        async def _boot() -> None:
            last_err: Exception | None = None
            test_ids = {drop.test_channel_id} if drop.test_channel_id else set()
            if promo.test_channel_id:
                test_ids.add(promo.test_channel_id)
            for i, token in enumerate(tokens, start=1):
                monitor = DropUserMonitor(
                    token,
                    channel_ids=channels,
                    test_channel_ids=test_ids,
                    role_id=drop.role_id,
                    on_drop=on_drop,
                    on_raw=on_raw,
                    debug=True,
                )
                try:
                    me = await monitor.probe_auth()
                    print(
                        f"[drop-monitor] auth ok token#{i} · "
                        f"{me.get('username')}#{me.get('discriminator')} ({me.get('id')})"
                    )
                    self._drop_monitor = monitor
                    monitor.start()
                    print(
                        f"[drop-monitor] started · channels {sorted(channels)} · "
                        f"test={sorted(test_ids)} · promo={sorted(promo_channels)} · "
                        f"promo_role={promo.role_id or 'unset'}"
                    )
                    asyncio.create_task(self.promo.warm())
                    await self._watch_send(
                        content=(
                            f"`[drop-debug]` monitor online as **{me.get('username')}** · "
                            f"watching `{', '.join(str(c) for c in sorted(channels))}` · "
                            f"promo channels `{', '.join(str(c) for c in sorted(promo_channels)) or '—'}` · "
                            f"promo role `{promo.role_id or 'unset'}`"
                        )
                    )
                    return
                except Exception as e:
                    last_err = e
                    print(f"[drop-monitor] token#{i} failed: {e}")
                    await monitor.stop()
            print(f"[drop-monitor] all tokens failed: {last_err}")
            await self._watch_send(content=f"`[drop-debug]` monitor failed to start · `{last_err}`")

        asyncio.create_task(_boot())


    async def _limited_confirmed(
        self,
        item: dict[str, Any],
        drop_msg: discord.Message | None,
        *,
        limited_checks: int | None = None,
    ) -> None:
        item_id = int(item.get("id") or 0)
        owner_id = self.settings.wallet_owner_id
        ping = f"<@{owner_id}>"

        try:
            balance = await self.economy.balance()
        except Exception:
            balance = None
        thumb = await self.api.asset_thumbnail(item_id)
        cache_stub = self.cache.find_stub(item_id)
        pages = build_inspect_pages(
            item,
            stub=cache_stub,
            thumbnail=thumb,
            balance=balance,
            limited_checks=limited_checks,
            limited_check_max=limited_check_max() if limited_checks else None,
        )
        view = InspectView(
            self,
            item=item,
            pages=pages,
            owner_id=owner_id,
            allow_buy=True,
        )

        if drop_msg is not None:
            try:
                await drop_msg.edit(embed=pages[0], view=view)
                view.message = drop_msg
            except Exception as e:
                print(f"[madoka] drop edit failed `{item_id}`: {e}")
        await self._watch_send(content=ping)
        print(f"[madoka] limited confirmed `{item_id}` {item.get('name')}")

    async def _update_drop_checks(
        self,
        item: dict[str, Any],
        drop_msg: discord.Message | None,
        attempt: int,
    ) -> None:
        item_id = int(item.get("id") or 0)
        name = str(item.get("name") or item_id)
        kind = limited_kind_label(item)
        sales = int(item.get("saleCount") or 0)
        serials = item.get("serialCount")
        left = remaining_serials(item)
        serials_text = f"{int(serials):,}" if serials is not None else "—"
        left_text = f"{left:,}" if left is not None else "—"
        checks_max = limited_check_max()
        await self._watch_send(
            content=(
                f"Limited check **{attempt}/{checks_max}** · "
                f"[{name}]({catalog_item(item_id, name)}) · "
                f"**{kind}** · sales `{sales:,}` · serials `{serials_text}` · left `{left_text}`"
            )
        )
        if drop_msg is None:
            return
        try:
            balance = await self.economy.balance()
        except Exception:
            balance = None
        thumb = await self.api.asset_thumbnail(item_id)
        cache_stub = self.cache.find_stub(item_id)
        pages = build_inspect_pages(
            item,
            stub=cache_stub,
            thumbnail=thumb,
            balance=balance,
            limited_checks=attempt,
            limited_check_max=checks_max,
        )
        view = InspectView(
            self,
            item=item,
            pages=pages,
            owner_id=self.settings.wallet_owner_id,
            allow_buy=True,
        )
        try:
            await drop_msg.edit(embed=pages[0], view=view)
            view.message = drop_msg
        except Exception as e:
            print(f"[madoka] drop check edit failed `{item_id}`: {e}")

    async def _announce_snipe(self, item: dict[str, Any], result: dict[str, Any]) -> None:
        if not result.get("purchased"):
            return
        item_id = int(item.get("id") or result.get("item_id") or 0)
        name = str(result.get("name") or item.get("name") or item_id)
        price = result.get("price")
        price_text = f"{int(price):,} R$" if price is not None else "—"
        serial = result.get("serial")
        serial_text = str(serial) if serial is not None else "—"
        await self._watch_send(
            content=(
                f"[{name}]({catalog_item(item_id, name)}) has been sniped for "
                f"**{price_text}** got serial **{serial_text}**"
            )
        )

    def _start_auto_snipe(self, item: dict[str, Any], drop_msg: discord.Message | None = None) -> None:
        if is_trap_item(item):
            return
        if not can_be_limited(item):
            return

        use_dual = self.settings.drop.enabled

        async def _on_check(attempt: int, fresh: dict[str, Any]) -> None:
            await self._update_drop_checks(fresh, drop_msg, attempt)

        async def _on_limited(fresh: dict[str, Any], attempt: int) -> None:
            await self._limited_confirmed(fresh, drop_msg, limited_checks=attempt)
            if use_dual:
                print(f"[dual-flag] catalog limited ready `{fresh.get('id')}` · {self.dual_flags.debug_flags(int(fresh.get('id') or 0))}")
                await self.dual_flags.on_catalog_limited(fresh)

        async def _run() -> None:
            try:
                if use_dual and is_limited(item):
                    await self.dual_flags.on_catalog_limited(item)
                    return

                result = await watch_limited_then_snipe(
                    self.economy,
                    item=item,
                    refresh_item=self._refresh_item,
                    on_check=_on_check if not is_limited(item) else None,
                    on_limited=_on_limited if not is_limited(item) else None,
                    buy=not use_dual,
                )
                if use_dual:
                    # watch_limited_then_snipe only buys when not dual; if limited mid-check,
                    # on_limited already armed catalog flag.
                    if result.get("limited_checks") and drop_msg is not None and not is_limited(item):
                        if not result.get("purchased"):
                            fresh = await self._refresh_item(int(item.get("id") or 0))
                            if fresh:
                                await self._update_drop_checks(
                                    fresh,
                                    drop_msg,
                                    int(result.get("limited_checks") or limited_check_max()),
                                )
                    return

                if result.get("purchased"):
                    await self._announce_snipe(item, result)
                elif result.get("limited_checks") and drop_msg is not None and not is_limited(item):
                    fresh = await self._refresh_item(int(item.get("id") or 0))
                    if fresh:
                        await self._update_drop_checks(
                            fresh,
                            drop_msg,
                            int(result.get("limited_checks") or limited_check_max()),
                        )
            except Exception as e:
                print(f"[auto-snipe] error `{item.get('id')}`: {e}")

        asyncio.create_task(_run())

    async def _announce_drop(self, stub: dict[str, Any]) -> None:
        item_id = int(stub.get("id") or 0)
        item = await self.catalog.lookup_id(item_id, force=False)
        if not item:
            rows = await self.catalog.fetch_fresh_stubs([stub])
            if not rows:
                return
            item = rows[0]
        skip = self._skip_drop(item)
        if skip:
            print(f"[madoka] skip `{item_id}` {item.get('name')} ({skip})")
            return

        owner_id = self.settings.wallet_owner_id

        try:
            balance = await self.economy.balance()
        except Exception:
            balance = None

        thumb = await self.api.asset_thumbnail(item_id)
        cache_stub = self.cache.find_stub(item_id)
        pages = build_inspect_pages(item, stub=cache_stub, thumbnail=thumb, balance=balance)
        view = InspectView(
            self,
            item=item,
            pages=pages,
            owner_id=owner_id,
            allow_buy=True,
        )
        ping = self._drop_ping(item)
        drop_msg = await self._watch_send(content=ping, embed=pages[0], view=view)
        self._start_auto_snipe(item, drop_msg)
        print(f"[madoka] drop `{item_id}` {item.get('name')}")

    async def _send_minute_report(self) -> None:
        window = self._poll_window
        polls = window.polls
        if polls <= 0:
            return

        added_rows = await self._resolve_items(list(window.added.values()), fresh=True)
        changed_rows = await self._resolve_items(list(window.changed.values()), fresh=True)
        removed_rows = await self._resolve_items(list(window.removed.values()))

        embed = build_minute_report_embed(
            polls=polls,
            added=added_rows,
            changed=changed_rows,
            removed=removed_rows,
            stats=self.catalog.stats(),
        )

        ping_items = [
            item for item in added_rows + changed_rows
            if self._skip_drop(item) is None and is_limited(item)
        ]
        ping = f"<@{self.settings.wallet_owner_id}>" if ping_items else None

        await self._watch_send(content=ping, embed=embed)
        print(
            f"[madoka] minute report · polled {polls} · "
            f"+{len(added_rows)} ~{len(changed_rows)} -{len(removed_rows)}"
        )

    @tasks.loop(seconds=1)
    async def watch_loop(self) -> None:
        try:
            self._poll_window.polls += 1
            diff = await self.catalog.refresh(force=False)

            if diff is not None:
                total = len(diff.added) + len(diff.changed) + len(diff.removed)
                if total > 0:
                    self._poll_window.merge(diff)
                    if self.settings.debug:
                        print(
                            f"[madoka] poll +{len(diff.added)} "
                            f"~{len(diff.changed)} -{len(diff.removed)}"
                        )
                    if diff.changed:
                        changed_stubs = [new for _old, new in diff.changed]
                        if self.settings.debug:
                            print(f"[madoka] refreshed details for {len(changed_stubs)} changed item(s)")
                    if diff.added:
                        for stub in diff.added:
                            await self._announce_drop(stub)
                    missing = len(self.catalog.cache.missing_stubs())
                    if missing > 0 and self.settings.debug:
                        print(f"[madoka] details missing after poll: {missing}")

            report_every = max(
                1,
                int(round(self.settings.poll_report_seconds / self.settings.poll_interval_seconds)),
            )
            if self._poll_window.polls >= report_every:
                await self._send_minute_report()
                self._poll_window.reset()
                self.dual_flags.prune()
        except Exception as e:
            print(f"[madoka] poll cycle failed: {e}")

    @watch_loop.error
    async def watch_loop_error(self, error: BaseException) -> None:
        print(f"[madoka] watch loop crashed: {error}")

    @watch_loop.before_loop
    async def before_watch_loop(self) -> None:
        await self.wait_until_ready()


@app_commands.command(name="inspect", description="Inspect a catalog item by id or name")
@app_commands.describe(query="Asset id or name")
async def inspect_cmd(interaction: discord.Interaction, query: str) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    q = (query or "").strip()
    if not q:
        await interaction.response.send_message("Provide an id or name.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        mode, rows = await bot.catalog.resolve_query(q)
    except Exception as e:
        await interaction.followup.send(f"Inspect failed: `{e}`", ephemeral=True)
        return

    if mode == "empty":
        await interaction.followup.send("Provide an id or name.", ephemeral=True)
        return
    if mode == "missing" or not rows:
        await interaction.followup.send(f"No catalog item found for `{q}`.", ephemeral=True)
        return
    if mode == "pick":
        allow_buy = can_use_wallet(interaction, bot.settings)
        view = InspectPickView(
            bot,
            query=q,
            results=rows,
            allow_buy=allow_buy,
            owner_id=interaction.user.id,
        )
        msg = await interaction.followup.send(
            content=f"**{len(rows)}** matches for `{q}`. Pick one:",
            view=view,
        )
        view.message = msg
        return

    await send_inspect(interaction, bot, rows[0])


@app_commands.command(name="balance", description="Check the bot wallet robux and tickets")
async def balance_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    if not can_use_wallet(interaction, bot.settings):
        await deny_wallet(interaction)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        payload = await bot.economy.balance()
    except Exception as e:
        await interaction.followup.send(f"Balance failed: `{e}`", ephemeral=True)
        return

    await interaction.followup.send(embed=build_balance_embed(payload), ephemeral=True)


buy_group = app_commands.Group(name="buy", description="Purchase catalog items")


@buy_group.command(name="free", description="Buy every free on-sale catalog item")
async def buy_free_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    if not can_use_wallet(interaction, bot.settings):
        await deny_wallet(interaction)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    if bot._buy_free_task is not None and not bot._buy_free_task.done():
        await interaction.followup.send(
            "Buy free is already running in the background.",
            ephemeral=True,
        )
        return

    print("[buy free] command started")
    try:
        print("[buy free] scanning free on-sale items...")
        free_items = await bot.catalog.free_items()
        print(f"[buy free] found {len(free_items)} free on-sale item(s)")
    except Exception as e:
        print(f"[buy free] catalog scan failed: {e}")
        await interaction.followup.send(f"Catalog scan failed: `{e}`", ephemeral=True)
        return

    if not free_items:
        print("[buy free] nothing to buy")
        await interaction.followup.send("No free on-sale items found in the catalog.", ephemeral=True)
        return

    async def _run(items: list[dict[str, Any]]) -> None:
        # Own HTTP session so purchase traffic never resets the poll/snipe client.
        buy_http = MadxkaHttp(bot.settings.cookie)
        buy_economy = EconomyService(buy_http)
        try:
            await bot._watch_send(
                content=f"`[buy free]` started · `{len(items)}` free item(s) · monitoring stays live"
            )
            outcome = await buy_economy.purchase_all_free(items)
            print("[buy free] command finished")
            embed = build_buy_free_embed(outcome)
            await bot._watch_send(embed=embed)
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                print(f"[buy free] discord followup failed (run still completed): {e}")
        except asyncio.CancelledError:
            print("[buy free] cancelled")
            raise
        except Exception as e:
            print(f"[buy free] failed: {e}")
            await bot._watch_send(content=f"`[buy free]` failed · `{e}`")
            try:
                await interaction.followup.send(f"Buy free failed: `{e}`", ephemeral=True)
            except Exception:
                pass
        finally:
            await buy_http.close()

    bot._buy_free_task = asyncio.create_task(_run(free_items), name="buy-free")
    await interaction.followup.send(
        f"Buy free running in background · `{len(free_items)}` items · "
        "catalog poll + drop monitor stay live.",
        ephemeral=True,
    )


redeem_group = app_commands.Group(name="redeem", description="Promocode tools")


@redeem_group.command(name="existing", description="Redeem all parsed codes from promo channels")
async def redeem_existing_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    if not can_use_wallet(interaction, bot.settings):
        await deny_wallet(interaction)
        return

    if bot._drop_monitor is None:
        await interaction.response.send_message(
            "Drop/promo monitor is offline · cannot read promo channel history.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    print("[promo] redeem existing started")
    try:
        outcome = await bot.promo.redeem_existing(limit=100)
    except Exception as e:
        print(f"[promo] redeem existing failed: {e}")
        await interaction.followup.send(f"Redeem existing failed: `{e}`", ephemeral=True)
        return

    embed = build_promo_redeem_embed(outcome)
    await bot._watch_send(embed=embed)
    await interaction.followup.send(embed=embed, ephemeral=True)
    print(
        f"[promo] redeem existing done · codes={len(outcome.get('codes') or [])} · "
        f"claimed={outcome.get('claimed')} failed={outcome.get('failed')}"
    )


catalog_group = app_commands.Group(name="catalog", description="Catalog cache and sync")


@catalog_group.command(name="stats", description="Show cached catalog stats")
async def catalog_stats(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)
    await interaction.response.send_message(embed=build_catalog_stats_embed(bot.catalog.stats()))


@catalog_group.command(name="refresh", description="Force refresh the catalog snapshot")
async def catalog_refresh(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    if not _is_admin(interaction):
        await interaction.response.send_message("Administrator or server owner only.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        diff = await bot.catalog.refresh(force=True)
        await bot.catalog.warm_details()
    except Exception as e:
        await interaction.followup.send(f"Refresh failed: `{e}`", ephemeral=True)
        return

    if diff is None:
        await interaction.followup.send(
            embed=build_catalog_stats_embed(bot.catalog.stats()),
            content="Catalog unchanged.",
        )
        return

    await interaction.followup.send(
        embed=build_diff_embed(
            added=await bot.catalog.fetch_fresh_stubs(diff.added),
            changed_count=len(diff.changed),
            removed_count=len(diff.removed),
        )
    )


test_group = app_commands.Group(name="test", description="Testing commands")


@test_group.command(name="random", description="View a random catalog item")
async def test_random(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    await interaction.response.defer(thinking=True)
    try:
        row = await bot.catalog.random_item()
    except Exception as e:
        await interaction.followup.send(f"Random item failed: `{e}`", ephemeral=True)
        return

    if not row:
        await interaction.followup.send("Catalog is empty.", ephemeral=True)
        return

    await send_inspect(interaction, bot, row)


ignore_group = app_commands.Group(name="ignore", description="Ignore catalog items by name keyword")


@ignore_group.command(name="add", description="Ignore items whose name contains a keyword")
@app_commands.describe(keyword="Case-insensitive substring match on item name")
async def ignore_add_cmd(interaction: discord.Interaction, keyword: str) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    ok, result = bot.ignore.add(keyword)
    if not ok:
        if result == "empty":
            await interaction.response.send_message("Keyword cannot be empty.", ephemeral=True)
            return
        await interaction.response.send_message(f"`{keyword.strip().lower()}` is already ignored.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Added ignore keyword `{result}`.",
        ephemeral=True,
    )


@ignore_group.command(name="remove", description="Stop ignoring a keyword")
@app_commands.describe(keyword="Keyword to remove")
async def ignore_remove_cmd(interaction: discord.Interaction, keyword: str) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    ok, result = bot.ignore.remove(keyword)
    if not ok:
        await interaction.response.send_message(f"`{keyword.strip().lower()}` is not in the ignore list.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Removed ignore keyword `{result}`.",
        ephemeral=True,
    )


@ignore_group.command(name="list", description="List ignored name keywords")
async def ignore_list_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    keywords = bot.ignore.list_keywords()
    if not keywords:
        await interaction.response.send_message("No ignore keywords set.", ephemeral=True)
        return

    lines = "\n".join(f"`{k}`" for k in keywords)
    await interaction.response.send_message(
        f"**{len(keywords)}** ignore keyword(s):\n{lines}",
        ephemeral=True,
    )


check_group = app_commands.Group(name="check", description="Check account data")


@check_group.command(name="inventory", description="Browse your inventory by item type")
async def check_inventory_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, MadokaBot)

    if not can_use_wallet(interaction, bot.settings):
        await deny_wallet(interaction)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        user = await bot.economy.session_user()
    except Exception as e:
        await interaction.followup.send(f"Session lookup failed: `{e}`", ephemeral=True)
        return

    uid = int(user.get("id") or 0)
    name = str(user.get("displayName") or user.get("name") or "?").strip() or "?"
    view = InventoryTypeView(
        bot,
        owner_id=interaction.user.id,
        user_id=uid,
        user_name=name,
    )
    msg = await interaction.followup.send(
        content=f"Pick an item type for **{name}** (`{uid}`):",
        view=view,
        ephemeral=True,
    )
    view.message = msg
