from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from promo.parser import extract_promo_codes, parse_promo_message
from promo.settings_types import PromoMonitorSettings

Notify = Callable[[str], Awaitable[None]]
FetchAround = Callable[[int, int], Awaitable[list[dict[str, Any]]]]
FetchRecent = Callable[[int, int], Awaitable[list[dict[str, Any]]]]


def _msg_content(raw: dict[str, Any]) -> str:
    return str(raw.get("content") or "")


class PromoSnipeService:
    def __init__(
        self,
        http,
        settings: PromoMonitorSettings,
        *,
        notify: Notify | None = None,
        fetch_around: FetchAround | None = None,
        fetch_recent: FetchRecent | None = None,
    ) -> None:
        self.http = http
        self.settings = settings
        self.notify = notify
        self.fetch_around = fetch_around
        self.fetch_recent = fetch_recent
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()

    def _log(self, msg: str) -> None:
        print(f"[promo] {msg}")

    async def _say(self, msg: str) -> None:
        self._log(msg)
        if self.notify is not None:
            try:
                await self.notify(msg)
            except Exception as e:
                self._log(f"notify failed: {e}")

    async def warm(self) -> None:
        try:
            token = await self.http.refresh_promo_token()
            self._log("token warm " + ("ok" if token else "failed"))
        except Exception as e:
            self._log(f"token warm failed: {e}")

    async def on_discord_message(self, raw: dict[str, Any]) -> None:
        channel_id = int(raw.get("channel_id") or 0)
        if channel_id not in self.settings.watched_channel_ids():
            return

        content = _msg_content(raw)
        message_id = int(raw.get("id") or 0)
        is_test = self.settings.is_test(channel_id)
        hit = parse_promo_message(
            content=content,
            channel_id=channel_id,
            message_id=message_id,
            role_id=self.settings.role_id,
            is_test=is_test,
        )
        preview = content.replace("\n", " | ")[:120]
        self._log(
            f"heard msg={message_id} ch={channel_id}"
            f"{' TEST' if is_test else ''} ping={hit.has_ping} "
            f"codes={list(hit.codes) or '—'} · {preview}"
        )

        # Official channel: if role id is set, only act on pinged drops
        # (or explicit `promocode:` labels). Test channel never requires ping.
        role_required = self.settings.role_id is not None and not is_test
        labeled = "promocode:" in content.lower()
        if role_required and not hit.has_ping and not labeled:
            self._log(f"skip msg={message_id} · waiting for promo role ping")
            return

        codes = list(hit.codes)

        # Codes in-message → redeem instantly (don't wait on Discord notify / neighbors).
        if codes:
            self._log(f"snipe now · {codes}")
            asyncio.create_task(
                self.redeem_many(
                    codes,
                    source=f"ch={channel_id} msg={message_id}",
                    instant=True,
                ),
                name=f"promo-snipe-{message_id}",
            )
            return

        # Ping present but no code in-body → check 2 messages above/below.
        if hit.has_ping and self.fetch_around is not None and message_id:
            try:
                nearby = await self.fetch_around(channel_id, message_id)
            except Exception as e:
                self._log(f"nearby fetch failed: {e}")
                nearby = []
            codes = self._codes_from_neighbors(nearby, message_id, allow_mixed=True)
            if codes:
                self._log(f"snipe nearby · {codes}")
                asyncio.create_task(
                    self.redeem_many(
                        codes,
                        source=f"ch={channel_id} msg={message_id} nearby",
                        instant=True,
                    ),
                    name=f"promo-snipe-near-{message_id}",
                )
                return
            self._log(
                f"ping without code · ch={channel_id} msg={message_id} · {preview}"
            )
            return

        self._log(f"no code in msg={message_id}")

    def _codes_from_neighbors(
        self,
        rows: list[dict[str, Any]],
        center_id: int,
        *,
        allow_mixed: bool,
    ) -> list[str]:
        ordered = sorted(rows, key=lambda r: int(r.get("id") or 0))
        ids = [int(r.get("id") or 0) for r in ordered]
        if center_id not in ids:
            found: list[str] = []
            for row in ordered:
                found.extend(extract_promo_codes(_msg_content(row), allow_mixed=allow_mixed))
            return list(dict.fromkeys(found))

        idx = ids.index(center_id)
        window = ordered[max(0, idx - 2) : idx + 3]
        found = []
        for row in window:
            mid = int(row.get("id") or 0)
            if mid == center_id:
                continue
            found.extend(extract_promo_codes(_msg_content(row), allow_mixed=allow_mixed))
        return list(dict.fromkeys(found))

    async def redeem_many(
        self,
        codes: list[str],
        *,
        source: str = "",
        force: bool = False,
        instant: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for code in codes:
            key = code.upper()
            async with self._lock:
                if not force and key in self._seen:
                    results.append(
                        {
                            "code": key,
                            "ok": False,
                            "status": "skipped",
                            "detail": "already attempted",
                        }
                    )
                    continue
                self._seen.add(key)

            # Live snipes: log + redeem first, Discord notify after (don't block POST).
            self._log(f"redeeming `{key}`" + (f" · {source}" if source else ""))
            try:
                outcome = await self.http.redeem_promocode(key)
            except Exception as e:
                outcome = {"ok": False, "status": "error", "detail": str(e), "code": key}
                await self._say(f"`{key}` · failed · `{e}`")
            else:
                status = str(outcome.get("status") or ("ok" if outcome.get("ok") else "failed"))
                detail = str(outcome.get("detail") or "")
                mark = "claimed" if outcome.get("ok") else status
                line = f"`{key}` · **{mark}**" + (f" · {detail}" if detail else "")
                if instant:
                    self._log(line)
                    if self.notify is not None:
                        asyncio.create_task(self.notify(line))
                else:
                    await self._say(line)
            results.append(outcome)
            if not instant:
                await asyncio.sleep(0.35)
        return results

    async def redeem_existing(self, *, limit: int = 100) -> dict[str, Any]:
        if self.fetch_recent is None:
            raise RuntimeError("fetch_recent not configured")

        channels = sorted(self.settings.watched_channel_ids())
        if not channels:
            raise RuntimeError("no promo channels configured")

        collected: list[str] = []
        scanned = 0
        for channel_id in channels:
            rows = await self.fetch_recent(channel_id, limit)
            scanned += len(rows)
            for row in rows:
                collected.extend(extract_promo_codes(_msg_content(row), allow_mixed=True))

        codes = list(dict.fromkeys(collected))
        self._log(f"existing · scanned {scanned} msg(s) · {len(codes)} code(s)")
        results = await self.redeem_many(codes, source="existing", force=True, instant=False)
        claimed = sum(1 for r in results if r.get("ok"))
        failed = len(results) - claimed
        return {
            "scanned_messages": scanned,
            "codes": codes,
            "results": results,
            "claimed": claimed,
            "failed": failed,
        }
