from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
from typing import Any

import aiohttp
from yarl import URL

from madxka.inventory import parse_inventory_payload
from madxka.urls import API, SITE
from madxka.thumbnails import THUMB_BATCH, THUMB_FORMAT, THUMB_SIZE, parse_thumb_rows

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MADXKA_ORIGIN = URL("https://madxka.com/")
PROMO_PAGE = f"{SITE}/internal/promocodes"


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
        self._promo_token: str | None = None
        self._promo_token_lock = asyncio.Lock()

    async def _reset_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

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
        await self._reset_session()

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
        url = f"{host}{path}"
        verb = method.upper()
        last_err: BaseException | None = None

        for attempt in range(2):
            await self.start()
            assert self._session is not None

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

            try:
                status, text, csrf, payload = await _do(extra_headers)
                if status == 403 and verb in MUTATING and csrf:
                    status, text, _csrf, payload = await _do({"X-CSRF-TOKEN": csrf})

                if status >= 400:
                    if status >= 500:
                        await self._reset_session()
                    raise MadxkaApiError(status, url, text)
                if payload is not None:
                    return payload
                return text
            except MadxkaApiError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last_err = e
                await self._reset_session()
                if attempt == 0:
                    continue
                raise

        if last_err:
            raise last_err
        raise RuntimeError("request failed")

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

    async def inventory_list(
        self,
        user_id: int,
        asset_type_id: int,
        *,
        cursor: str = "",
        items_per_page: int = 99999,
    ) -> dict[str, Any]:
        payload = await self._request(
            SITE,
            "GET",
            "/users/inventory/list-json",
            params={
                "userId": int(user_id),
                "assetTypeId": int(asset_type_id),
                "cursor": cursor or "",
                "itemsPerPage": int(items_per_page),
            },
        )
        if not isinstance(payload, dict):
            return {"items": [], "next_cursor": None, "total": 0}
        items, next_cursor, total = parse_inventory_payload(payload)
        return {"items": items, "next_cursor": next_cursor, "total": total}

    async def inventory_all(
        self,
        user_id: int,
        asset_type_id: int,
        *,
        items_per_page: int = 99999,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page = await self.inventory_list(
                user_id,
                asset_type_id,
                cursor=cursor,
                items_per_page=items_per_page,
            )
            batch = page.get("items") or []
            if isinstance(batch, list):
                out.extend(batch)
            nxt = page.get("next_cursor")
            if not nxt or not batch:
                break
            cursor = str(nxt)
        return out

    async def user_owns_asset(
        self,
        user_id: int,
        asset_id: int,
        *,
        asset_type_id: int | None = None,
    ) -> bool:
        # asset_type_id kept for call-site compat; never dump full inventory
        # for a boolean owns check (that starves the bot event loop).
        _ = asset_type_id
        asset_id = int(asset_id)
        if not asset_id:
            return False
        payload = await self._request(
            API,
            "GET",
            f"/inventory/v1/users/{int(user_id)}/items/Asset/{asset_id}",
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

    async def redeem_promocode(self, code: str) -> dict[str, Any]:
        code = (code or "").strip().upper()
        if not code:
            return {"ok": False, "status": "error", "detail": "empty code", "code": code}

        await self.start()
        assert self._session is not None

        # Prefer a warm antiforgery token so live snipes can POST immediately.
        token = self._promo_token
        if not token:
            token = await self.refresh_promo_token()
        if not token:
            raise RuntimeError("promocode antiforgery token missing")

        outcome = await self._post_promo_redeem(code, token)
        # Refresh in background for the next snipe; retry once if antiforgery failed.
        asyncio.create_task(self.refresh_promo_token())
        if outcome.get("status") == "antiforgery":
            token = await self.refresh_promo_token()
            if not token:
                raise RuntimeError("promocode antiforgery token missing")
            outcome = await self._post_promo_redeem(code, token)
            asyncio.create_task(self.refresh_promo_token())
        return outcome

    async def refresh_promo_token(self) -> str | None:
        async with self._promo_token_lock:
            await self.start()
            assert self._session is not None
            async with self._session.get(
                PROMO_PAGE,
                headers={"Accept": "text/html,application/xhtml+xml"},
            ) as resp:
                html = await resp.text()
                if resp.status >= 400:
                    self._promo_token = None
                    raise MadxkaApiError(resp.status, PROMO_PAGE, html)
            token = _extract_antiforgery_token(html)
            self._promo_token = token
            return token

    async def _post_promo_redeem(self, code: str, token: str) -> dict[str, Any]:
        assert self._session is not None
        form = {
            "__RequestVerificationToken": token,
            "code": code,
            "handler": "redeem",
        }
        # Consume cached token so the next redeem refreshes if needed.
        if self._promo_token == token:
            self._promo_token = None
        async with self._session.post(
            PROMO_PAGE,
            data=form,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": SITE,
                "Referer": PROMO_PAGE,
            },
            allow_redirects=True,
        ) as resp:
            body = await resp.text()
            status_code = resp.status
        return _parse_promo_redeem_result(code, status_code, body)


def _extract_antiforgery_token(html: str) -> str | None:
    # Attributes can appear in any order, e.g.
    # <input name="__RequestVerificationToken" type="hidden" value="..." />
    patterns = (
        r'<input[^>]*\bname=["\']__RequestVerificationToken["\'][^>]*\bvalue=["\']([^"\']+)["\']',
        r'<input[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bname=["\']__RequestVerificationToken["\']',
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']',
    )
    for pat in patterns:
        m = re.search(pat, html or "", re.I)
        if m:
            token = (m.group(1) or "").strip()
            if token:
                return token
    return None


def _parse_promo_redeem_result(code: str, status: int, html: str) -> dict[str, Any]:
    raw = html or ""
    alert = _extract_alert_text(raw)
    blob = alert or raw
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", blob))
    text = re.sub(r"\s+", " ", text).strip()
    lowered = text.lower()
    raw_l = raw.lower()

    def hit(*needles: str) -> bool:
        return any(n in lowered for n in needles)

    if status >= 400:
        return {
            "ok": False,
            "status": "http_error",
            "detail": f"http {status}",
            "code": code,
        }
    if hit("antiforgery", "verification token", "request verification"):
        return {"ok": False, "status": "antiforgery", "detail": "bad antiforgery token", "code": code}
    if "alert-success" in raw_l or hit(
        "successfully redeemed",
        "redeemed successfully",
        "code redeemed",
        "you redeemed",
        "promo code redeemed",
    ):
        return {"ok": True, "status": "claimed", "detail": "redeemed", "code": code}

    if hit("already redeemed", "already claimed", "already used", "you have already"):
        return {"ok": False, "status": "already", "detail": "already redeemed", "code": code}
    if hit(
        "no longer active",
        "no longer valid",
        "not active",
        "is expired",
        "has expired",
        "expired",
    ):
        return {"ok": False, "status": "inactive", "detail": "promocode is no longer active", "code": code}
    if hit(
        "maximum redemption",
        "max redemption",
        "out of uses",
        "no uses left",
        "fully redeemed",
        "max uses",
    ):
        return {"ok": False, "status": "exhausted", "detail": "maximum redemptions reached", "code": code}
    if hit("invalid code", "does not exist", "not found", "unknown code", "not valid"):
        return {"ok": False, "status": "invalid", "detail": "invalid code", "code": code}
    if "alert-danger" in raw_l or "alert-warning" in raw_l:
        detail = alert or text
        detail = re.sub(r"^(Promocodes\s+)+", "", detail, flags=re.I).strip()
        return {"ok": False, "status": "rejected", "detail": (detail or "rejected")[:160], "code": code}

    snippet = alert or text
    snippet = re.sub(r"^(Promocodes\s+Back to madxka\.com\s+Promocodes\s+Account Deletion\s+Promocodes\s+)+", "", snippet, flags=re.I)
    snippet = re.sub(r"^(Promocodes\s+)+", "", snippet, flags=re.I).strip()
    return {"ok": False, "status": "unknown", "detail": (snippet or f"http {status}")[:160], "code": code}


def _extract_alert_text(html: str) -> str | None:
    m = re.search(
        r'<div[^>]*class=["\'][^"\']*alert[^"\']*["\'][^>]*>(.*?)</div>',
        html or "",
        re.I | re.S,
    )
    if not m:
        return None
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
