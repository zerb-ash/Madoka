from __future__ import annotations

import asyncio
from typing import Any

from madxka.http import MadxkaHttp
from economy.guard import is_purchase_blocked, purchase_block_reason


CURRENCY_ROBUX = 1
CURRENCY_TICKETS = 2


def purchase_payload(item: dict[str, Any], *, currency: int = CURRENCY_ROBUX) -> dict[str, Any]:
    asset_id = int(item.get("id") or 0)
    seller = item.get("creatorTargetId")
    if seller is None and isinstance(item.get("lowestSellerData"), dict):
        seller = item["lowestSellerData"].get("sellerId")

    if currency == CURRENCY_TICKETS:
        price = item.get("priceTickets")
        if price is None:
            price = 0
    else:
        price = item.get("price")
        if price is None:
            price = item.get("lowestPrice")
        if price is None:
            price = 0

    return {
        "assetId": asset_id,
        "expectedPrice": int(price),
        "expectedSellerId": int(seller or 1),
        "userAssetId": None,
        "expectedCurrency": int(currency),
    }


def _log(msg: str) -> None:
    print(f"[buy free] {msg}")


class EconomyService:
    def __init__(self, http: MadxkaHttp) -> None:
        self.http = http
        self._user: dict[str, Any] | None = None

    async def session_user(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._user is not None and not refresh:
            return self._user
        self._user = await self.http.authenticated_user()
        return self._user

    async def balance(self) -> dict[str, Any]:
        user = await self.session_user()
        uid = int(user.get("id") or 0)
        currency = await self.http.user_currency(uid)
        return {
            "user": user,
            "robux": int(currency.get("robux") or 0),
            "tickets": int(currency.get("tickets") or 0),
        }

    async def purchase(
        self,
        item: dict[str, Any],
        *,
        currency: int = CURRENCY_ROBUX,
    ) -> dict[str, Any]:
        asset_id = int(item.get("id") or 0)
        if not asset_id:
            raise ValueError("missing asset id")
        block = purchase_block_reason(item)
        if block:
            name = str(item.get("name") or asset_id)
            print(f"[purchase] blocked `{asset_id}` {name} · matched /{block}/")
            return {
                "request": None,
                "result": {},
                "purchased": False,
                "reason": "blocked dangerous item name",
            }
        body = purchase_payload(item, currency=currency)
        result = await self.http.purchase_product(asset_id, body)
        return {
            "request": body,
            "result": result,
            "purchased": bool(result.get("purchased")),
            "reason": str(result.get("reason") or ""),
        }

    async def purchase_all_free(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        bought: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[tuple[dict[str, Any], str]] = []

        user = await self.session_user()
        uid = int(user.get("id") or 0)
        total = len(items)
        _log(f"start · user {user.get('name')} ({uid}) · {total} free item(s)")

        for i, item in enumerate(items, start=1):
            asset_id = int(item.get("id") or 0)
            name = str(item.get("name") or asset_id)
            label = f"[{i}/{total}] `{asset_id}` {name}"

            try:
                owned = await self.http.user_owns_asset(uid, asset_id)
            except Exception as e:
                _log(f"{label} · own-check failed: {e}")
                failed.append((item, f"own-check failed: {e}"))
                await asyncio.sleep(0.08)
                continue

            if owned:
                _log(f"{label} · skip (already owned)")
                skipped.append(item)
                await asyncio.sleep(0.05)
                continue

            if is_purchase_blocked(item):
                _log(f"{label} · skip (blocked dangerous name)")
                skipped.append(item)
                await asyncio.sleep(0.05)
                continue

            _log(f"{label} · buying...")
            try:
                outcome = await self.purchase(item)
            except Exception as e:
                err = str(e)
                if "already owned" in err.lower():
                    _log(f"{label} · skip (already owned)")
                    skipped.append(item)
                else:
                    _log(f"{label} · failed: {err}")
                    failed.append((item, err))
                await asyncio.sleep(0.12)
                continue

            if outcome.get("purchased"):
                _log(f"{label} · bought")
                bought.append(item)
            else:
                reason = str(outcome.get("reason") or "")
                if "already owned" in reason.lower():
                    _log(f"{label} · skip (already owned)")
                    skipped.append(item)
                else:
                    _log(f"{label} · declined: {reason or 'purchase declined'}")
                    failed.append((item, reason or "purchase declined"))

            await asyncio.sleep(0.12)

        _log(
            f"done · scanned {total} · bought {len(bought)} · "
            f"skipped {len(skipped)} · failed {len(failed)}"
        )
        return {
            "scanned": total,
            "bought": bought,
            "skipped": skipped,
            "failed": failed,
        }
