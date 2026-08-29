from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from bot.auth import can_use_wallet, deny_runner, deny_wallet, is_runner
from bot.embeds import build_inspect_pages, build_purchase_embed
from economy.guard import is_purchase_blocked
from economy.service import CURRENCY_ROBUX, CURRENCY_TICKETS

if TYPE_CHECKING:
    from bot.client import MadokaBot


async def _check_runner(interaction: discord.Interaction, owner_id: int) -> bool:
    if is_runner(interaction, owner_id):
        return True
    await deny_runner(interaction)
    return False


def _robux_price(item: dict[str, Any]) -> int | None:
    if item.get("price") is not None:
        return int(item["price"])
    if item.get("lowestPrice") is not None:
        return int(item["lowestPrice"])
    return None


def _tix_price(item: dict[str, Any]) -> int | None:
    if item.get("priceTickets") is None:
        return None
    return int(item["priceTickets"])


def _can_buy_robux(item: dict[str, Any]) -> bool:
    if item.get("isForSale") is False:
        return False
    return _robux_price(item) is not None


def _can_buy_tix(item: dict[str, Any]) -> bool:
    if item.get("isForSale") is False:
        return False
    return _tix_price(item) is not None


async def _inspect_payload(
    bot: MadokaBot,
    item: dict[str, Any],
    *,
    show_wallet: bool = False,
) -> tuple[list[discord.Embed], dict[str, Any] | None, str | None]:
    item_id = int(item.get("id") or 0)
    stub = bot.cache.find_stub(item_id)
    thumb = await bot.api.asset_thumbnail(item_id)
    balance: dict[str, Any] | None = None
    if show_wallet:
        try:
            balance = await bot.economy.balance()
        except Exception:
            balance = None
    pages = build_inspect_pages(item, stub=stub, thumbnail=thumb, balance=balance)
    return pages, stub, thumb


class BuyButton(discord.ui.Button):
    def __init__(
        self,
        *,
        bot: MadokaBot,
        item: dict[str, Any],
        owner_id: int,
        currency: int,
    ) -> None:
        self.bot = bot
        self.item = item
        self.owner_id = owner_id
        self.currency = currency
        offsale = item.get("isForSale") is False
        blocked = is_purchase_blocked(item)

        if currency == CURRENCY_TICKETS:
            tix = _tix_price(item)
            label = "Buy with tix" if tix is None else f"Buy with tix · {tix:,}"
            enabled = _can_buy_tix(item) and not blocked
        else:
            price = _robux_price(item)
            if price is None:
                label = "Buy"
            elif price == 0:
                label = "Buy · Free"
            else:
                label = f"Buy · {price:,} R$"
            enabled = _can_buy_robux(item) and not blocked

        if blocked:
            label = "Blocked"
            enabled = False
        elif offsale:
            label = "Offsale"
            enabled = False

        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.success if currency == CURRENCY_ROBUX else discord.ButtonStyle.primary,
            disabled=not enabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _check_runner(interaction, self.owner_id):
            return
        if not can_use_wallet(interaction, self.bot.settings):
            await deny_wallet(interaction)
            return
        if is_purchase_blocked(self.item):
            await interaction.response.send_message(
                "This item is blocked from purchase.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            outcome = await self.bot.economy.purchase(self.item, currency=self.currency)
        except Exception as e:
            await interaction.followup.send(f"Purchase failed: `{e}`", ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_purchase_embed(self.item, outcome),
            ephemeral=True,
        )


class InspectView(discord.ui.View):
    def __init__(
        self,
        bot: MadokaBot,
        *,
        item: dict[str, Any],
        pages: list[discord.Embed],
        owner_id: int,
        allow_buy: bool = True,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.item = item
        self.pages = pages
        self.owner_id = owner_id
        if allow_buy and not is_purchase_blocked(item):
            if _can_buy_robux(item) or item.get("isForSale") is False:
                self.add_item(
                    BuyButton(
                        bot=bot,
                        item=item,
                        owner_id=owner_id,
                        currency=CURRENCY_ROBUX,
                    )
                )
            if _can_buy_tix(item):
                self.add_item(
                    BuyButton(
                        bot=bot,
                        item=item,
                        owner_id=owner_id,
                        currency=CURRENCY_TICKETS,
                    )
                )


class InspectPickView(discord.ui.View):
    def __init__(
        self,
        bot: MadokaBot,
        *,
        query: str,
        results: list[dict[str, Any]],
        owner_id: int,
        allow_buy: bool = False,
        timeout: float = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.query = query
        self.results = results
        self.owner_id = owner_id
        self.allow_buy = allow_buy
        options: list[discord.SelectOption] = []
        for item in results[:25]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            robux = _robux_price(item)
            tix = _tix_price(item)
            if item.get("isForSale") is False:
                desc = "Offsale"
            elif robux is not None and tix is not None:
                desc = f"{robux:,} R$ / {tix:,} tix"
            elif tix is not None:
                desc = f"{tix:,} tix"
            elif robux is not None:
                desc = f"{robux:,} R$" if robux else "Free"
            else:
                desc = "—"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    description=f"ID {item_id} · {desc}"[:100],
                    value=str(item_id),
                )
            )
        select = discord.ui.Select(
            placeholder=f"Pick an item for '{query[:40]}'",
            options=options,
            row=0,
        )
        select.callback = self._on_pick
        self.pick = select
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if not await _check_runner(interaction, self.owner_id):
            return
        assert isinstance(interaction.data, dict)
        values = interaction.data.get("values") or []
        if not values:
            await interaction.response.defer()
            return
        item_id = int(values[0])
        item = next((r for r in self.results if int(r.get("id") or 0) == item_id), None)
        if item is None:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return
        allow_buy = self.allow_buy and can_use_wallet(interaction, self.bot.settings)
        await interaction.response.defer(thinking=True)
        pages, _stub, _thumb = await _inspect_payload(
            self.bot,
            item,
            show_wallet=allow_buy,
        )
        view = InspectView(
            self.bot,
            item=item,
            pages=pages,
            owner_id=self.owner_id,
            allow_buy=allow_buy,
        )
        await interaction.edit_original_response(embed=pages[0], view=view)


async def send_inspect(
    interaction: discord.Interaction,
    bot: MadokaBot,
    item: dict[str, Any],
) -> None:
    allow_buy = can_use_wallet(interaction, bot.settings)
    pages, _stub, _thumb = await _inspect_payload(bot, item, show_wallet=allow_buy)
    view = InspectView(
        bot,
        item=item,
        pages=pages,
        owner_id=interaction.user.id,
        allow_buy=allow_buy,
    )
    await interaction.followup.send(embed=pages[0], view=view)
