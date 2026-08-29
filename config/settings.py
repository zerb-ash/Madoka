from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

CACHE_FILES = ("catalog.json", "catalog-meta.json", "details-cache.json")


def _resolve_data_dir() -> Path:
    raw = (os.getenv("DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return ROOT / "data"


def ensure_data_dir(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    seed = ROOT / "data"
    if data_dir.resolve() == seed.resolve():
        return data_dir

    for name in CACHE_FILES:
        dest = data_dir / name
        src = seed / name
        if dest.is_file() or not src.is_file():
            continue
        shutil.copy2(src, dest)
    return data_dir


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    cookie: str
    discord_guild_id: int | None
    watch_channel_id: int | None
    poll_interval_minutes: int
    debug: bool
    wallet_owner_id: int
    data_dir: Path
    catalog_path: Path
    catalog_meta_path: Path
    details_path: Path


def load_settings() -> Settings:
    guild = os.getenv("DISCORD_GUILD_ID", "").strip()
    watch = os.getenv("WATCH_CHANNEL_ID", "").strip()
    owner = os.getenv("WALLET_OWNER_ID", "1521237044746125462").strip()
    data = ensure_data_dir(_resolve_data_dir())
    return Settings(
        discord_token=(os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip(),
        cookie=(os.getenv("COOKIE") or "").strip(),
        discord_guild_id=int(guild) if guild.isdigit() else None,
        watch_channel_id=int(watch) if watch.isdigit() else None,
        poll_interval_minutes=max(1, int(os.getenv("POLL_INTERVAL_MINUTES", "1"))),
        debug=os.getenv("DEBUG", "").strip().lower() in {"1", "true", "yes", "on"},
        wallet_owner_id=int(owner) if owner.isdigit() else 1521237044746125462,
        data_dir=data,
        catalog_path=data / "catalog.json",
        catalog_meta_path=data / "catalog-meta.json",
        details_path=data / "details-cache.json",
    )
