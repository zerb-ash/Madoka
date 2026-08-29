from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from madxka.http import MadxkaHttp
from store.cache import CatalogCache


DETAILS_BATCH = 100


def is_free_item(item: dict[str, Any]) -> bool:
    if item.get("isForSale") is False:
        return False
    price = item.get("price")
    if price is None:
        return False
    return int(price) == 0


@dataclass(frozen=True, slots=True)
class CatalogDiff:
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    changed: list[tuple[dict[str, Any], dict[str, Any]]]


def _stub_key(stub: dict[str, Any]) -> str:
    item_type = str(stub.get("itemType") or "Asset")
    item_id = int(stub.get("id") or 0)
    return f"{item_type}:{item_id}"


def _stub_map(stubs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for stub in stubs:
        key = _stub_key(stub)
        if key:
            out[key] = stub
    return out


def catalog_hash(stubs: list[dict[str, Any]]) -> str:
    ordered = sorted(stubs, key=lambda s: (_stub_key(s),))
    raw = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def diff_catalog(
    old_stubs: list[dict[str, Any]],
    new_stubs: list[dict[str, Any]],
) -> CatalogDiff:
    old_map = _stub_map(old_stubs)
    new_map = _stub_map(new_stubs)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for key, stub in new_map.items():
        prev = old_map.get(key)
        if prev is None:
            added.append(stub)
            continue
        if json.dumps(prev, sort_keys=True) != json.dumps(stub, sort_keys=True):
            changed.append((prev, stub))

    for key, stub in old_map.items():
        if key not in new_map:
            removed.append(stub)

    return CatalogDiff(added=added, removed=removed, changed=changed)


class CatalogService:
    def __init__(self, http: MadxkaHttp, cache: CatalogCache) -> None:
        self.http = http
        self.cache = cache
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        if self.cache.stubs:
            return
        await self.refresh(force=False)

    async def warm_start(self) -> None:
        await self.refresh(force=True)

    async def refresh(self, *, force: bool = False) -> CatalogDiff | None:
        new_stubs = await self.http.search_items()
        new_hash = catalog_hash(new_stubs)

        async with self._lock:
            old_stubs = list(self.cache.stubs)
            old_hash = self.cache.meta.get("hash")

            if not force and old_hash == new_hash and old_stubs:
                self.cache.touch()
                self.cache.save()
                return None

            diff = diff_catalog(old_stubs, new_stubs) if old_stubs else None

            self.cache.set_catalog(new_stubs, new_hash)
            if diff is not None:
                for key in {_stub_key(s) for s in diff.removed}:
                    self.cache.drop_details(key)
                for _prev, stub in diff.changed:
                    self.cache.drop_details(_stub_key(stub))

            self.cache.save()
            return diff

    async def fetch_details(self, stubs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not stubs:
            return []
        entries = [
            {"id": int(s["id"]), "itemType": str(s.get("itemType") or "Asset")}
            for s in stubs
            if s.get("id") is not None
        ]
        out: list[dict[str, Any]] = []
        for i in range(0, len(entries), DETAILS_BATCH):
            chunk = entries[i : i + DETAILS_BATCH]
            rows = await self.http.item_details(chunk)
            out.extend(rows)
            if i + DETAILS_BATCH < len(entries):
                await asyncio.sleep(0.15)
        return out

    async def hydrate_stubs(self, stubs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        ready: list[dict[str, Any]] = []
        for stub in stubs:
            key = _stub_key(stub)
            cached = self.cache.get_details(key)
            if cached and self.cache.stub_matches(key, stub):
                ready.append(cached)
            else:
                missing.append(stub)

        if missing:
            fetched = await self.fetch_details(missing)
            by_id = {int(row.get("id") or 0): row for row in fetched}
            async with self._lock:
                for stub in missing:
                    row = by_id.get(int(stub.get("id") or 0))
                    if row:
                        self.cache.put_details(_stub_key(stub), stub, row)
                        ready.append(row)
                self.cache.save()

        return ready

    async def lookup_inspect(self, item_id: int) -> tuple[dict[str, Any], dict[str, Any] | None, str | None] | None:
        row = await self.lookup_id(item_id)
        if not row:
            return None
        stub = self.cache.find_stub(item_id)
        thumb = await self.http.asset_thumbnail(item_id)
        return row, stub, thumb

    async def resolve_query(self, query: str, *, limit: int = 25) -> tuple[str, list[dict[str, Any]]]:
        raw = (query or "").strip()
        if not raw:
            return "empty", []

        if raw.isdigit():
            payload = await self.lookup_inspect(int(raw))
            if not payload:
                return "missing", []
            item, _stub, _thumb = payload
            return "single", [item]

        hits = await self.search_name(raw, limit=limit)
        if not hits:
            return "missing", []
        if len(hits) == 1:
            return "single", hits
        return "pick", hits

    async def lookup_id(self, item_id: int) -> dict[str, Any] | None:
        await self.ensure_loaded()
        stub = self.cache.find_stub(item_id)
        if stub is None:
            diff = await self.refresh(force=True)
            stub = self.cache.find_stub(item_id)
            if stub is None:
                return None
            if diff and diff.added:
                await self.hydrate_stubs([stub])
                return self.cache.get_details(_stub_key(stub))

        key = _stub_key(stub)
        cached = self.cache.get_details(key)
        if cached and self.cache.stub_matches(key, stub):
            return cached

        rows = await self.fetch_details([stub])
        if not rows:
            return cached
        row = rows[0]
        async with self._lock:
            self.cache.put_details(key, stub, row)
            self.cache.save()
        return row

    async def search_name(self, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
        await self.ensure_loaded()
        q = query.strip().lower()
        if not q:
            return []

        hits: list[dict[str, Any]] = []
        need: list[dict[str, Any]] = []

        for stub in self.cache.stubs:
            key = _stub_key(stub)
            cached = self.cache.get_details(key)
            if cached and self.cache.stub_matches(key, stub):
                name = str(cached.get("name") or "").lower()
                if q in name:
                    hits.append(cached)
            else:
                need.append(stub)

        if need and len(hits) < limit:
            for i in range(0, len(need), DETAILS_BATCH):
                chunk = need[i : i + DETAILS_BATCH]
                rows = await self.hydrate_stubs(chunk)
                for row in rows:
                    name = str(row.get("name") or "").lower()
                    if q in name:
                        hits.append(row)
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break

        hits.sort(key=lambda r: str(r.get("name") or "").lower())
        return hits[:limit]

    async def random_item(self) -> dict[str, Any] | None:
        await self.ensure_loaded()
        if not self.cache.stubs:
            return None
        stub = random.choice(self.cache.stubs)
        rows = await self.hydrate_stubs([stub])
        return rows[0] if rows else None

    async def free_items(self) -> list[dict[str, Any]]:
        await self.ensure_loaded()
        free: list[dict[str, Any]] = []
        need: list[dict[str, Any]] = []

        for stub in self.cache.stubs:
            key = _stub_key(stub)
            cached = self.cache.get_details(key)
            if cached and self.cache.stub_matches(key, stub):
                if is_free_item(cached):
                    free.append(cached)
            else:
                need.append(stub)

        for i in range(0, len(need), DETAILS_BATCH):
            chunk = need[i : i + DETAILS_BATCH]
            rows = await self.hydrate_stubs(chunk)
            for row in rows:
                if is_free_item(row):
                    free.append(row)
            if i + DETAILS_BATCH < len(need):
                await asyncio.sleep(0.15)

        free.sort(key=lambda r: int(r.get("id") or 0))
        return free

    def stats(self) -> dict[str, Any]:
        meta = self.cache.meta
        fetched = meta.get("fetched_at")
        return {
            "count": len(self.cache.stubs),
            "hash": meta.get("hash"),
            "fetched_at": fetched,
            "checked_at": meta.get("checked_at"),
            "details_cached": len(self.cache.details),
        }
