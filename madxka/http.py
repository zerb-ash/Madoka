from __future__ import annotations

import json
from typing import Any

import aiohttp
from yarl import URL

from madxka.urls import API
from madxka.thumbnails import THUMB_BATCH, THUMB_FORMAT, THUMB_SIZE, parse_thumb_rows

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MADXKA_ORIGIN = URL("https://madxka.com/")


class MadxkaApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"madxka {status} {url}: {body[:200]}")


def format_cookie(raw: str) -> str:
    blob = (raw or "").strip()
    if not blob:
        return ""
    if ";" in blob or ".ROBLOSECURITY=" in blob:
        return blob
    return f".ROBLOSECURITY={blob}"


def parse_cookies(raw: str) -> dict[str, str]:
    blob = format_cookie(raw)
    if not blob:
        return {}
    out: dict[str, str] = {}
    for part in blob.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        key = name.strip()
        if key:
            out[key] = value.strip()
    return out


class MadxkaHttp:
    def __init__(self, cookie: str) -> None:
        self._cookie_raw = (cookie or "").strip()
        self.cookie = format_cookie(self._cookie_raw)
        self._session: aiohttp.ClientSession | None = None
        self._thumb_cache: dict[int, str | None] = {}

    async def start(self) -> None:
        if self._session and not self._session.closed:
            return
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        jar = aiohttp.CookieJar(unsafe=True)
        cookies = parse_cookies(self._cookie_raw)
        if cookies:
            jar.update_cookies(cookies, response_url=MADXKA_ORIGIN)
        self._session = aiohttp.ClientSession(
            headers=headers,
            cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=45, connect=8),
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        host: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        await self.start()
        assert self._session is not None
        url = f"{host}{path}"
        verb = method.upper()

        async def _do(extra: dict[str, str] | None = None) -> tuple[int, str, str, Any | None]:
            headers = extra or None
            async with self._session.request(
                verb,
                url,
                params=params,
                json=body,
                headers=headers,
            ) as resp:
                text = await resp.text()
                payload: Any | None = None
                if "json" in (resp.content_type or ""):
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = None
                return resp.status, text, resp.headers.get("x-csrf-token") or "", payload

        status, text, csrf, payload = await _do(extra_headers)
        if status == 403 and verb in MUTATING and csrf:
            status, text, _csrf, payload = await _do({"X-CSRF-TOKEN": csrf})

        if status >= 400:
            raise MadxkaApiError(status, url, text)
        if payload is not None:
            return payload
        return text

    async def search_items(self, *, limit: int = 99999) -> list[dict[str, Any]]:
        payload = await self._request(API, "GET", "/catalog/v1/search/items", params={"limit": limit})
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        return list(data) if isinstance(data, list) else []

    async def item_details(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not entries:
            return []
        payload = await self._request(
            API,
            "POST",
            "/catalog/v1/catalog/items/details",
            body={"items": entries},
        )
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        return list(data) if isinstance(data, list) else []

    async def asset_thumbnails(
        self,
        asset_ids: list[int],
        *,
        size: str = THUMB_SIZE,
        force: bool = False,
    ) -> dict[int, str | None]:
        want: list[int] = []
        seen: set[int] = set()
        for raw in asset_ids:
            asset_id = int(raw)
            if not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            if force or asset_id not in self._thumb_cache:
                want.append(asset_id)

        for i in range(0, len(want), THUMB_BATCH):
            chunk = want[i : i + THUMB_BATCH]
            payload = await self._request(
                API,
                "GET",
                "/thumbnails/v1/assets",
                params={
                    "assetIds": ",".join(str(asset_id) for asset_id in chunk),
                    "format": THUMB_FORMAT,
                    "size": size,
                },
            )
            rows: list[Any] = []
            if isinstance(payload, dict):
                raw_rows = payload.get("data")
                if isinstance(raw_rows, list):
                    rows = raw_rows
            parsed = parse_thumb_rows(rows)
            for asset_id in chunk:
                self._thumb_cache[asset_id] = parsed.get(asset_id)

        out: dict[int, str | None] = {}
        for raw in asset_ids:
            asset_id = int(raw)
            if asset_id:
                out[asset_id] = self._thumb_cache.get(asset_id)
        return out

    async def asset_thumbnail(
        self,
        asset_id: int,
        *,
        size: str = THUMB_SIZE,
        force: bool = False,
    ) -> str | None:
        rows = await self.asset_thumbnails([int(asset_id)], size=size, force=force)
        return rows.get(int(asset_id))

    async def authenticated_user(self) -> dict[str, Any]:
        payload = await self._request(API, "GET", "/users/v1/users/authenticated")
        if not isinstance(payload, dict):
            raise MadxkaApiError(500, f"{API}/users/v1/users/authenticated", "bad payload")
        return payload

    async def user_currency(self, user_id: int) -> dict[str, Any]:
        payload = await self._request(API, "GET", f"/economy/v1/users/{int(user_id)}/currency")
        if not isinstance(payload, dict):
            raise MadxkaApiError(500, f"{API}/economy/v1/users/{user_id}/currency", "bad payload")
        return payload

    async def user_owns_asset(self, user_id: int, asset_id: int) -> bool:
        payload = await self._request(
            API,
            "GET",
            f"/inventory/v1/users/{int(user_id)}/items/Asset/{int(asset_id)}",
        )
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        return isinstance(data, list) and len(data) > 0

    async def purchase_product(self, asset_id: int, purchase_body: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request(
            API,
            "POST",
            f"/economy/v1/purchases/products/{int(asset_id)}",
            body=purchase_body,
        )
        if not isinstance(payload, dict):
            raise MadxkaApiError(500, f"{API}/economy/v1/purchases/products/{asset_id}", "bad payload")
        return payload
