from __future__ import annotations

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
    build_poll_embed,
    build_poll_ok_embed,
)
from bot.views import InspectPickView, InspectView, send_inspect
from catalog.service import CatalogDiff, CatalogService
from config.settings import Settings
from economy.service import EconomyService
from madxka.http import MadxkaHttp
from store.cache import CatalogCache


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
        self._watch_channel_cache: discord.abc.Messageable | None = None

    def _register_commands(self) -> None:
        self.tree.add_command(inspect_cmd)
        self.tree.add_command(balance_cmd)
        self.tree.add_command(buy_group)
        self.tree.add_command(catalog_group)
        self.tree.add_command(test_group)

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

        self.watch_loop.change_interval(minutes=self.settings.poll_interval_minutes)
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
    ) -> None:
        channel = await self._watch_channel()
        if channel is None:
            print("[madoka] watch send skipped: no channel")
            return
        try:
            await channel.send(content=content, embed=embed, view=view)
        except Exception as e:
            print(f"[madoka] watch send failed: {e}")
            self._watch_channel_cache = None

    async def _announce_ok(self) -> None:
        await self._watch_send(embed=build_poll_ok_embed(stats=self.catalog.stats()))

    async def _announce_diff(self, diff: CatalogDiff) -> None:
        total = len(diff.added) + len(diff.changed) + len(diff.removed)
        if total <= 0:
            return

        print(
            f"[madoka] poll +{len(diff.added)} "
            f"~{len(diff.changed)} -{len(diff.removed)}"
        )

        channel = await self._watch_channel()
        if channel is None:
            print("[madoka] poll announce skipped: no channel")
            return

        added_ids = {int(s.get("id") or 0) for s in diff.added}
        targets = diff.added + [new for _old, new in diff.changed]
        details = await self.catalog.hydrate_stubs(targets)
        added_rows = [row for row in details if int(row.get("id") or 0) in added_ids]

        poll_embed = build_poll_embed(
            added=added_rows,
            changed_count=len(diff.changed),
            removed_count=len(diff.removed),
            stats=self.catalog.stats(),
        )
        owner_id = self.settings.wallet_owner_id
        await self._watch_send(content=f"<@{owner_id}>", embed=poll_embed)

        if not added_rows:
            return

        for item in added_rows[:5]:
            try:
                balance = await self.economy.balance()
            except Exception:
                balance = None
            item_id = int(item.get("id") or 0)
            stub = self.cache.find_stub(item_id)
            thumb = await self.api.asset_thumbnail(item_id)
            pages = build_inspect_pages(item, stub=stub, thumbnail=thumb, balance=balance)
            view = InspectView(
                self,
                item=item,
                pages=pages,
                owner_id=owner_id,
                allow_buy=True,
            )
            await self._watch_send(embed=pages[0], view=view)

        if len(added_rows) > 5:
            await self._watch_send(embed=discord.Embed(
                description=f"-# +{len(added_rows) - 5} more new items",
                color=0xFEE75C,
            ))

    @tasks.loop(minutes=1)
    async def watch_loop(self) -> None:
        try:
            diff = await self.catalog.refresh(force=False)

            if diff is None:
                if self.settings.debug:
                    print("[madoka] poll: no changes")
                await self._announce_ok()
                return

            total = len(diff.added) + len(diff.changed) + len(diff.removed)
            if total <= 0:
                if self.settings.debug:
                    print("[madoka] poll: hash changed but no diff rows")
                await self._announce_ok()
                return

            await self._announce_diff(diff)
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
        await interaction.followup.send(
            content=f"**{len(rows)}** matches for `{q}`. Pick one:",
            view=view,
        )
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
    print("[buy free] command started")
    try:
        print("[buy free] refreshing catalog...")
        await bot.catalog.refresh(force=True)
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

    try:
        outcome = await bot.economy.purchase_all_free(free_items)
    except Exception as e:
        print(f"[buy free] failed: {e}")
        await interaction.followup.send(f"Buy free failed: `{e}`", ephemeral=True)
        return

    print("[buy free] command finished")
    await interaction.followup.send(embed=build_buy_free_embed(outcome), ephemeral=True)


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
            added=await bot.catalog.hydrate_stubs(diff.added),
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
