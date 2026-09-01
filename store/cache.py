from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _item_key(stub: dict[str, Any]) -> str:
    return f"{str(stub.get('itemType') or 'Asset')}:{int(stub.get('id') or 0)}"


class CatalogCache:
    def __init__(
        self,
        *,
        catalog_path: Path,
        meta_path: Path,
        details_path: Path,
    ) -> None:
        self.catalog_path = catalog_path
        self.meta_path = meta_path
        self.details_path = details_path
        self.stubs: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self._stub_by_id: dict[int, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        if self.catalog_path.is_file():
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self.stubs = list(raw) if isinstance(raw, list) else []
        else:
            self.stubs = []

        if self.meta_path.is_file():
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self.meta = raw if isinstance(raw, dict) else {}
        else:
            self.meta = {}

        if self.details_path.is_file():
            raw = json.loads(self.details_path.read_text(encoding="utf-8"))
            self.details = raw if isinstance(raw, dict) else {}
        else:
            self.details = {}

        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._stub_by_id = {}
        for stub in self.stubs:
            item_id = stub.get("id")
            if item_id is not None:
                self._stub_by_id[int(item_id)] = stub

    def save(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.catalog_path,
            json.dumps(self.stubs, indent=2, ensure_ascii=False),
        )
        _atomic_write(
            self.meta_path,
            json.dumps(self.meta, indent=2, ensure_ascii=False),
        )
        _atomic_write(
            self.details_path,
            json.dumps(self.details, indent=2, ensure_ascii=False),
        )

    def touch(self) -> None:
        self.meta["checked_at"] = _utc_now()

    def set_catalog(self, stubs: list[dict[str, Any]], digest: str) -> None:
        self.stubs = list(stubs)
        self._rebuild_index()
        self.meta = {
            "hash": digest,
            "fetched_at": _utc_now(),
            "count": len(stubs),
            "checked_at": self.meta.get("checked_at"),
        }

    def find_stub(self, item_id: int) -> dict[str, Any] | None:
        return self._stub_by_id.get(int(item_id))

    def stub_matches(self, key: str, stub: dict[str, Any]) -> bool:
        row = self.details.get(key)
        if not row:
            return False
        saved = row.get("_stub")
        if not isinstance(saved, dict):
            return False
        return json.dumps(saved, sort_keys=True) == json.dumps(stub, sort_keys=True)

    def get_details(self, key: str) -> dict[str, Any] | None:
        row = self.details.get(key)
        if not row:
            return None
        detail = row.get("detail")
        return detail if isinstance(detail, dict) else None

    def put_details(self, key: str, stub: dict[str, Any], detail: dict[str, Any]) -> None:
        self.details[key] = {
            "_stub": stub,
            "detail": detail,
            "fetched_at": _utc_now(),
        }

    def drop_details(self, key: str) -> None:
        self.details.pop(key, None)

    def failed_detail_ids(self) -> set[int]:
        raw = self.meta.get("details_failed")
        if not isinstance(raw, list):
            return set()
        out: set[int] = set()
        for val in raw:
            try:
                out.add(int(val))
            except (TypeError, ValueError):
                continue
        return out

    def mark_details_failed(self, item_id: int) -> None:
        failed = self.failed_detail_ids()
        failed.add(int(item_id))
        self.meta["details_failed"] = sorted(failed)

    def is_details_failed(self, item_id: int) -> bool:
        return int(item_id) in self.failed_detail_ids()

    def missing_stubs(self) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        for stub in self.stubs:
            item_id = stub.get("id")
            if item_id is None:
                continue
            if self.is_details_failed(int(item_id)):
                continue
            key = _item_key(stub)
            cached = self.get_details(key)
            if cached and self.stub_matches(key, stub):
                continue
            missing.append(stub)
        return missing

    def iter_cached_items(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for stub in self.stubs:
            key = _item_key(stub)
            cached = self.get_details(key)
            if cached and self.stub_matches(key, stub) and not cached.get("_detailsUnavailable"):
                rows.append(cached)
        return rows
