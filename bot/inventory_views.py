from __future__ import annotations

from typing import Any, TYPE_CHECKING

import discord

from bot.panels import OwnerPanelView, PANEL_TIMEOUT
from catalog.restrictions import is_limited, is_limited_unique, normalize_item
from madxka.asset_types import ASSET_TYPE_NAMES, asset_type_name
from madxka.urls import catalog_item

if TYPE_CHECKING:
    from bot.client import MadokaBot


PER_PAGE = 8


def _catalog_meta(bot: MadokaBot, asset_id: int) -> dict[str, Any] | None:
    stub = bot.cache.find_stub(asset_id)
    key = f"Asset:{asset_id}"
    detail = bot.cache.get_details(key)
    if not detail:
        return None
    return normalize_item(detail, stub)


def _row_price_text(catalog: dict[str, Any] | None, row: dict[str, Any]) -> str:
    if catalog:
        if catalog.get("isForSale") is False:
            lowest = catalog.get("lowestPrice")
            if lowest is not None:
                return f"{int(lowest):,} R$ resale"
            return "Offsale"
        price = catalog.get("price")
        if price is not None:
            return "Free" if int(price) == 0 else f"{int(price):,} R$"
    owned = row.get("price_in_robux")
    if owned is not None:
        return "Free" if int(owned) == 0 else f"{int(owned):,} R$ (owned)"
    return "—"


def _row_limited_text(catalog: dict[str, Any] | None) -> str:
    if not catalog:
        return "unknown"
    if is_limited_unique(catalog):
        return "Limited U"
    if is_limited(catalog):
        return "Limited"
    return "Regular"


def _row_sale_text(catalog: dict[str, Any] | None) -> str:
    if not catalog or catalog.get("isForSale") is None:
        return "unknown"
    return "yes" if catalog.get("isForSale") else "no"


def enrich_rows(bot: MadokaBot, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        asset_id = int(row.get("asset_id") or 0)
        catalog = _catalog_meta(bot, asset_id)
        out.append({**row, "catalog": catalog})
    return out


def build_inventory_embed(
    *,
    asset_type_id: int,
    rows: list[dict[str, Any]],
    page: int,
    total_items: int,
    user_name: str,
) -> discord.Embed:
    pages = max(1, (len(rows) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PER_PAGE : (page + 1) * PER_PAGE]

    embed = discord.Embed(
        title=f"Inventory · {asset_type_name(asset_type_id)}",
        description=f"**{user_name}** · {total_items:,} item(s) · page {page + 1}/{pages}",
        color=0x5865F2,
    )

    if not chunk:
        embed.add_field(name="Items", value="No items in this category.", inline=False)
        return embed

    lines: list[str] = []
    for row in chunk:
        asset_id = int(row.get("asset_id") or 0)
        name = str(row.get("name") or asset_id)
        catalog = row.get("catalog")
        limited = _row_limited_text(catalog if isinstance(catalog, dict) else None)
        sale = _row_sale_text(catalog if isinstance(catalog, dict) else None)
        price = _row_price_text(catalog if isinstance(catalog, dict) else None, row)
        serial = row.get("serial_number")
        serial_bit = f" · serial `{serial}`" if serial is not None else ""
        lines.append(
            f"[{name}]({catalog_item(asset_id, name)}) · `{asset_id}`\n"
            f"-# {limited} · on sale {sale} · {price}{serial_bit}"
        )

    embed.add_field(name="Items", value="\n\n".join(lines)[:1024], inline=False)
    return embed


class InventoryListView(OwnerPanelView):
    def __init__(
        self,
        bot: MadokaBot,
        *,
        owner_id: int,
        asset_type_id: int,
        rows: list[dict[str, Any]],
        total_items: int,
        user_name: str,
        page: int = 0,
    ) -> None:
        super().__init__(owner_id=owner_id, timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.asset_type_id = asset_type_id
        self.rows = rows
        self.total_items = total_items
        self.user_name = user_name
        self.page = page
        self._sync_buttons()

    def _pages(self) -> int:
        return max(1, (len(self.rows) + PER_PAGE - 1) // PER_PAGE)

    def _sync_buttons(self) -> None:
        pages = self._pages()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "inv_prev":
                    child.disabled = self.page <= 0
                if child.custom_id == "inv_next":
                    child.disabled = self.page >= pages - 1

    def _embed(self) -> discord.Embed:
        return build_inventory_embed(
            asset_type_id=self.asset_type_id,
            rows=self.rows,
            page=self.page,
            total_items=self.total_items,
            user_name=self.user_name,
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, custom_id="inv_prev")
    async def prev_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        await self._edit(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="inv_next")
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.page < self._pages() - 1:
            self.page += 1
        await self._edit(interaction)


class InventoryTypeView(OwnerPanelView):
    def __init__(self, bot: MadokaBot, *, owner_id: int, user_id: int, user_name: str) -> None:
        super().__init__(owner_id=owner_id, timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.user_id = user_id
        self.user_name = user_name

        options: list[discord.SelectOption] = []
        for type_id in sorted(ASSET_TYPE_NAMES):
            label = ASSET_TYPE_NAMES[type_id]
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(type_id),
                    description=f"Asset type {type_id}"[:100],
                )
            )

        select = discord.ui.Select(
            placeholder="Pick an item type",
            options=options[:25],
            row=0,
        )
        select.callback = self._on_pick
        self.type_select = select
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        assert isinstance(interaction.data, dict)
        values = interaction.data.get("values") or []
        if not values:
            await interaction.response.defer()
            return

        asset_type_id = int(values[0])
        await interaction.response.defer(thinking=True)
        try:
            rows = await self.bot.api.inventory_all(self.user_id, asset_type_id)
        except Exception as e:
            await interaction.followup.send(f"Inventory fetch failed: `{e}`", ephemeral=True)
            return

        enriched = enrich_rows(self.bot, rows)
        view = InventoryListView(
            self.bot,
            owner_id=self.owner_id,
            asset_type_id=asset_type_id,
            rows=enriched,
            total_items=len(enriched),
            user_name=self.user_name,
        )
        embed = view._embed()
        msg = await interaction.edit_original_response(content=None, embed=embed, view=view)
        view.message = msg
