from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import discord

from madxka.urls import catalog_item, group_page, user_profile

if TYPE_CHECKING:
    from bot.client import MadokaBot


def _item_url(item: dict[str, Any]) -> str:
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    return catalog_item(item_id, name)


def _price_text(item: dict[str, Any]) -> str:
    if item.get("isForSale") is False:
        return "Offsale"
    price = item.get("price")
    tix = item.get("priceTickets")
    if price is not None:
        robux = f"Free" if int(price) == 0 else f"{int(price):,} R$"
        if tix is not None:
            return f"{robux} / {int(tix):,} tix"
        return robux
    lowest = item.get("lowestPrice")
    if lowest is not None:
        return f"{int(lowest):,} R$ (resale)"
    if tix is not None:
        return f"{int(tix):,} tix"
    return "—"


def _limited_text(item: dict[str, Any]) -> str:
    if item.get("isLimitedUnique"):
        serial = item.get("serialCount")
        if serial:
            return f"Limited U ({int(serial):,})"
        return "Limited U"
    if item.get("isLimited"):
        return "Limited"
    return "Regular"


def _creator_text(item: dict[str, Any]) -> str:
    name = str(item.get("creatorName") or "?")
    ctype = str(item.get("creatorType") or "")
    tid = item.get("creatorTargetId")
    if ctype == "Group" and tid:
        return f"[{name}]({group_page(int(tid))})"
    if tid:
        return f"[{name}]({user_profile(int(tid))})"
    return name


def _desc(text: str | None, limit: int = 280) -> str:
    raw = (text or "").strip()
    if not raw:
        return "-# no description"
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def _cap(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _discord_time(value: str | datetime | None, style: str = "R") -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        raw = str(value).strip()
        if not raw:
            return "—"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def _quote_block(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "> —"
    return "\n".join(f"> {line}" if line else ">" for line in raw.split("\n"))


def _md_line(label: str, value: str) -> str:
    return f"- **{label}:** {value}"


def _md_val(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, list):
        if not val:
            return "—"
        return ", ".join(str(x) for x in val)
    if isinstance(val, dict):
        if not val:
            return "—"
        text = json.dumps(val, ensure_ascii=False)
        if len(text) > 160:
            return f"`{text[:157]}…`"
        return f"`{text}`"
    return str(val)


def _inspect_body_md(item: dict[str, Any], item_id: int) -> str:
    parts: list[str] = []
    desc = str(item.get("description") or "").strip()
    if desc:
        parts.extend(["## Description", _quote_block(desc), ""])

    parts.append("## Item")
    parts.append(_md_line("ID", f"`{item_id}`"))
    parts.append(_md_line("Limited", _md_val(item.get("isLimited"))))
    parts.append(_md_line("Limited U", _md_val(item.get("isLimitedUnique"))))
    price = item.get("price")
    if price is None:
        parts.append(_md_line("Price", "—"))
    else:
        parts.append(_md_line("Price", "Free" if int(price) == 0 else f"{int(price):,} R$"))
    parts.append(_md_line("Ticket price", _md_val(item.get("priceTickets"))))
    serials = item.get("serialCount")
    parts.append(_md_line("Serials", f"{int(serials):,}" if serials is not None else "—"))
    parts.append(_md_line("For sale", _md_val(item.get("isForSale"))))
    parts.append(_md_line("Sales", f"{int(item.get('saleCount') or 0):,}"))

    return _cap("\n".join(parts))


def build_balance_embed(payload: dict[str, Any]) -> discord.Embed:
    user = payload.get("user") or {}
    uid = int(user.get("id") or 0)
    name = str(user.get("displayName") or user.get("name") or "?").strip() or "?"
    embed = discord.Embed(
        title=f"Balance · {name}",
        url=user_profile(uid) if uid else None,
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Robux", value=f"{int(payload.get('robux') or 0):,} R$", inline=True)
    embed.add_field(name="Tickets", value=str(int(payload.get("tickets") or 0)), inline=True)
    embed.add_field(name="User ID", value=str(uid), inline=True)
    return embed


def build_purchase_embed(item: dict[str, Any], outcome: dict[str, Any]) -> discord.Embed:
    result = outcome.get("result") or {}
    request = outcome.get("request") or {}
    ok = bool(outcome.get("purchased"))
    embed = discord.Embed(
        title="Purchase " + ("success" if ok else "failed"),
        color=0x57F287 if ok else 0xED4245,
        timestamp=datetime.now(timezone.utc),
    )
    item_id = int(item.get("id") or 0)
    name = str(item.get("name") or item_id)
    embed.description = f"[{name}]({_item_url(item)}) · `{item_id}`"
    embed.add_field(name="Reason", value=str(outcome.get("reason") or result.get("reason") or "?"), inline=False)
    currency = int(request.get("expectedCurrency") or result.get("currency") or 1)
    unit = "tix" if currency == 2 else "R$"
    paid = result.get("price")
    if paid is None:
        paid = request.get("expectedPrice")
    if paid is not None:
        embed.add_field(name="Paid", value=f"{int(paid):,} {unit}", inline=True)
    if result.get("sellerName"):
        embed.add_field(name="Seller", value=str(result["sellerName"]), inline=True)
    return embed


def build_catalog_stats_embed(stats: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Catalog",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Items", value=str(int(stats.get("count") or 0)), inline=True)
    embed.add_field(name="Details cached", value=str(int(stats.get("details_cached") or 0)), inline=True)

    digest = stats.get("hash")
    if digest:
        embed.add_field(name="Hash", value=f"`{digest[:16]}…`", inline=False)

    fetched = stats.get("fetched_at")
    if fetched:
        embed.add_field(name="Last fetch", value=_discord_time(fetched), inline=False)

    return embed


def build_diff_embed(
    *,
    added: list[dict[str, Any]],
    changed_count: int,
    removed_count: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Catalog update",
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="New", value=str(len(added)), inline=True)
    embed.add_field(name="Changed", value=str(changed_count), inline=True)
    embed.add_field(name="Removed", value=str(removed_count), inline=True)

    if added:
        lines: list[str] = []
        for item in added[:8]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            lines.append(f"[{name}]({_item_url(item)}) · `{item_id}`")
        if len(added) > 8:
            lines.append(f"-# +{len(added) - 8} more")
        embed.add_field(name="New items", value="\n".join(lines), inline=False)

    return embed


def build_minute_report_embed(
    *,
    polls: int,
    added: list[dict[str, Any]],
    changed: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Poll Minute Report",
        description=f"Polled **{polls:,}** time(s) this minute.",
        color=0x5865F2 if added or changed or removed else 0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Added", value=str(len(added)), inline=True)
    embed.add_field(name="Changed", value=str(len(changed)), inline=True)
    embed.add_field(name="Removed", value=str(len(removed)), inline=True)

    def _lines(items: list[dict[str, Any]], limit: int = 20) -> str:
        if not items:
            return "—"
        rows: list[str] = []
        for item in items[:limit]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            price = _price_text(item)
            rows.append(f"[{name}]({_item_url(item)}) · `{item_id}` · {price}")
        if len(items) > limit:
            rows.append(f"-# +{len(items) - limit} more")
        return "\n".join(rows)[:1024]

    if added:
        embed.add_field(name="Added items", value=_lines(added), inline=False)
    if changed:
        embed.add_field(name="Changed items", value=_lines(changed), inline=False)
    if removed:
        embed.add_field(name="Removed items", value=_lines(removed, limit=20), inline=False)

    if stats:
        bits: list[str] = []
        if stats.get("count") is not None:
            bits.append(f"{int(stats['count']):,} items tracked")
        digest = stats.get("hash")
        if digest:
            bits.append(f"hash `{str(digest)[:16]}`")
        if bits:
            embed.set_footer(text=" · ".join(bits))

    return embed


def build_poll_ok_embed(*, stats: dict[str, Any] | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="Catalog poll",
        description="No catalog changes detected. Watch is alive.",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc),
    )
    if stats:
        embed.add_field(name="Items tracked", value=f"{int(stats.get('count') or 0):,}", inline=True)
        digest = stats.get("hash")
        if digest:
            embed.add_field(name="Hash", value=f"`{str(digest)[:16]}`", inline=True)
        checked = stats.get("checked_at") or stats.get("fetched_at")
        if checked:
            embed.add_field(name="Last check", value=_discord_time(checked), inline=False)
    return embed


def build_buy_free_embed(outcome: dict[str, Any]) -> discord.Embed:
    bought = outcome.get("bought") or []
    skipped = outcome.get("skipped") or []
    failed = outcome.get("failed") or []
    scanned = int(outcome.get("scanned") or 0)

    embed = discord.Embed(
        title="Buy free",
        color=0x57F287 if bought else 0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Free on sale", value=str(scanned), inline=True)
    embed.add_field(name="Purchased", value=str(len(bought)), inline=True)
    embed.add_field(name="Already owned", value=str(len(skipped)), inline=True)
    embed.add_field(name="Failed", value=str(len(failed)), inline=True)

    if bought:
        lines: list[str] = []
        for item in bought[:12]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            lines.append(f"[{name}]({_item_url(item)}) · `{item_id}`")
        if len(bought) > 12:
            lines.append(f"-# +{len(bought) - 12} more")
        embed.add_field(name="Purchased items", value="\n".join(lines)[:1024], inline=False)

    if failed:
        lines = []
        for item, reason in failed[:5]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            short = reason.replace("\n", " ")[:120]
            lines.append(f"`{item_id}` {name[:40]} · {short}")
        if len(failed) > 5:
            lines.append(f"-# +{len(failed) - 5} more failures")
        embed.add_field(name="Failures", value="\n".join(lines)[:1024], inline=False)

    return embed


def build_poll_embed(
    *,
    added: list[dict[str, Any]],
    changed_count: int,
    removed_count: int,
    stats: dict[str, Any] | None = None,
) -> discord.Embed:
    new_count = len(added)
    embed = discord.Embed(
        title="Catalog poll",
        description="Catalog change detected.",
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="New", value=str(new_count), inline=True)
    embed.add_field(name="Changed", value=str(changed_count), inline=True)
    embed.add_field(name="Removed", value=str(removed_count), inline=True)

    if added:
        lines: list[str] = []
        for item in added[:10]:
            item_id = int(item.get("id") or 0)
            name = str(item.get("name") or item_id)
            price = _price_text(item)
            lines.append(f"[{name}]({_item_url(item)}) · `{item_id}` · {price}")
        if len(added) > 10:
            lines.append(f"-# +{len(added) - 10} more")
        embed.add_field(name="New items", value="\n".join(lines)[:1024], inline=False)

    if stats:
        bits: list[str] = []
        if stats.get("count") is not None:
            bits.append(f"{int(stats['count']):,} items tracked")
        digest = stats.get("hash")
        if digest:
            bits.append(f"hash `{str(digest)[:16]}`")
        if bits:
            embed.set_footer(text=" · ".join(bits))

    return embed


def _merged_item(item: dict[str, Any], stub: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(item)
    if stub:
        for key in ("isLimited", "isLimitedUnique"):
            if key in stub and key not in out:
                out[key] = stub[key]
    return out


def build_inspect_pages(
    item: dict[str, Any],
    *,
    stub: dict[str, Any] | None = None,
    thumbnail: str | None = None,
    balance: dict[str, Any] | None = None,
) -> list[discord.Embed]:
    merged = _merged_item(item, stub)
    item_id = int(merged.get("id") or 0)
    name = str(merged.get("name") or f"Item {item_id}")

    embed = discord.Embed(
        title=name,
        url=catalog_item(item_id, name),
        description=_inspect_body_md(merged, item_id),
        color=0xEB459E,
        timestamp=datetime.now(timezone.utc),
    )
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    footer_bits = [f"ID {item_id}"]
    if balance is not None:
        footer_bits.append(f"Balance {int(balance.get('robux') or 0):,} R$")
    embed.set_footer(text=" · ".join(footer_bits))

    return [embed]

