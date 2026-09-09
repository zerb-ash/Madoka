from __future__ import annotations

import asyncio
import json
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from drop.parser import DropMessage, parse_drop_message

DISCORD_API = "https://discord.com/api/v9"
GATEWAY_URL = f"{DISCORD_API}/gateway"

OnDrop = Callable[[DropMessage], Awaitable[None]]
OnRaw = Callable[[dict[str, Any], DropMessage], Awaitable[None]]


class DropUserMonitor:
    """Lightweight Discord user-gateway listener for drop channels."""

    def __init__(
        self,
        token: str,
        *,
        channel_ids: set[int],
        test_channel_ids: set[int] | None = None,
        role_id: int | None = None,
        on_drop: OnDrop | None = None,
        on_raw: OnRaw | None = None,
        debug: bool = False,
    ) -> None:
        self.token = (token or "").strip()
        self.channel_ids = set(channel_ids)
        self.test_channel_ids = set(test_channel_ids or ())
        self.role_id = role_id
        self.on_drop = on_drop
        self.on_raw = on_raw
        self.debug = debug
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self._seq: int | None = None
        self._heartbeat_interval = 41.25
        self._user: dict[str, Any] | None = None
        self._seen_messages = 0
        self._seen_drops = 0

    @property
    def user(self) -> dict[str, Any] | None:
        return self._user

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="drop-user-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch_recent(self, channel_id: int, *, limit: int = 25) -> list[dict[str, Any]]:
        await self._ensure_session()
        assert self._session is not None
        url = f"{DISCORD_API}/channels/{int(channel_id)}/messages"
        async with self._session.get(url, params={"limit": max(1, min(100, limit))}) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"discord {resp.status}: {text[:300]}")
            data = json.loads(text)
            return list(data) if isinstance(data, list) else []

    async def fetch_around(
        self,
        channel_id: int,
        message_id: int,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self._ensure_session()
        assert self._session is not None
        url = f"{DISCORD_API}/channels/{int(channel_id)}/messages"
        params = {
            "around": int(message_id),
            "limit": max(1, min(100, limit)),
        }
        async with self._session.get(url, params=params) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"discord {resp.status}: {text[:300]}")
            data = json.loads(text)
            rows = list(data) if isinstance(data, list) else []
            rows.sort(key=lambda r: int(r.get("id") or 0))
            return rows

    async def probe_auth(self) -> dict[str, Any]:
        await self._ensure_session()
        assert self._session is not None
        async with self._session.get(f"{DISCORD_API}/users/@me") as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"discord auth {resp.status}: {text[:300]}")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise RuntimeError("bad @me payload")
            return data

    async def _ensure_session(self) -> None:
        if self._session and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": self.token,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[drop-monitor] gateway error: {e}")
            if self._stop.is_set():
                break
            await asyncio.sleep(3)

    async def _connect_once(self) -> None:
        await self._ensure_session()
        assert self._session is not None
        async with self._session.get(GATEWAY_URL) as resp:
            payload = await resp.json()
            ws_url = str(payload.get("url") or "")
        if not ws_url:
            raise RuntimeError("no gateway url")

        url = f"{ws_url}?v=9&encoding=json"
        print(f"[drop-monitor] connecting…")
        async with self._session.ws_connect(url, heartbeat=None, max_msg_size=0) as ws:
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                async for msg in ws:
                    if self._stop.is_set():
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        raw = zlib.decompress(msg.data).decode("utf-8")
                        data = json.loads(raw)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                    elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        break
                    else:
                        continue

                    op = data.get("op")
                    event = data.get("t")
                    d = data.get("d")
                    if data.get("s") is not None:
                        self._seq = data["s"]

                    if op == 10:  # hello
                        self._heartbeat_interval = float((d or {}).get("heartbeat_interval", 41250)) / 1000.0
                        await ws.send_json(self._identify_payload())
                        if heartbeat_task:
                            heartbeat_task.cancel()
                        heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    elif op == 0 and event == "READY":
                        self._user = (d or {}).get("user") if isinstance(d, dict) else None
                        uname = (self._user or {}).get("username")
                        print(f"[drop-monitor] ready as {uname} · watching {sorted(self.channel_ids)}")
                    elif op == 0 and event == "MESSAGE_CREATE":
                        await self._on_message(d if isinstance(d, dict) else {})
                    elif op == 7:  # reconnect
                        break
                    elif op == 9:  # invalid session
                        await asyncio.sleep(2)
                        break
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()

    def _identify_payload(self) -> dict[str, Any]:
        return {
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": "en-US",
                    "browser_user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "browser_version": "120.0.0.0",
                    "os_version": "10",
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": 260000,
                    "client_event_source": None,
                },
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [],
                    "afk": False,
                },
                "compress": False,
                "client_state": {
                    "guild_versions": {},
                },
            },
        }

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send_json({"op": 1, "d": self._seq})
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[drop-monitor] heartbeat failed: {e}")

    async def _on_message(self, raw: dict[str, Any]) -> None:
        channel_id = int(raw.get("channel_id") or 0)
        if channel_id not in self.channel_ids:
            return
        message_id = int(raw.get("id") or 0)
        content = str(raw.get("content") or "")
        embeds = raw.get("embeds") if isinstance(raw.get("embeds"), list) else []
        author = ((raw.get("author") or {}) if isinstance(raw.get("author"), dict) else {})
        author_name = str(author.get("username") or "?")
        is_test = channel_id in self.test_channel_ids
        drop = parse_drop_message(
            content=content,
            embeds=embeds,
            channel_id=channel_id,
            message_id=message_id,
            role_id=self.role_id,
            is_test=is_test,
        )
        self._seen_messages += 1
        if drop.is_drop:
            self._seen_drops += 1

        preview = (content or "(embed/empty)").replace("\n", " | ")[:120]
        line = (
            f"[drop-monitor] heard #{self._seen_messages} msg={message_id} "
            f"ch={channel_id}{' TEST' if is_test else ''} author={author_name} "
            f"drop={drop.is_drop} ids={list(drop.item_ids)} ping={drop.has_ping} "
            f"drops_total={self._seen_drops} · {preview}"
        )
        print(line)

        if self.on_raw is not None:
            try:
                await self.on_raw(raw, drop)
            except Exception as e:
                print(f"[drop-monitor] on_raw failed: {e}")

        if not drop.is_drop:
            return
        if self.on_drop is None:
            return
        try:
            await self.on_drop(drop)
        except Exception as e:
            print(f"[drop-monitor] on_drop failed: {e}")
