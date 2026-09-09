from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from drop import DropMonitorSettings
from promo import PromoMonitorSettings

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


def _env_int(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.isdigit() else None


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    cookie: str
    discord_guild_id: int | None
    watch_channel_id: int | None
    poll_interval_seconds: float
    poll_report_seconds: float
    debug: bool
    wallet_owner_id: int
    data_dir: Path
    catalog_path: Path
    catalog_meta_path: Path
    details_path: Path
    ignore_path: Path
    drop: DropMonitorSettings
    promo: PromoMonitorSettings
    safebuy: bool


def load_settings() -> Settings:
    guild = os.getenv("DISCORD_GUILD_ID", "").strip()
    watch = os.getenv("WATCH_CHANNEL_ID", "").strip()
    owner = os.getenv("WALLET_OWNER_ID", "1521237044746125462").strip()
    poll_sec = os.getenv("POLL_INTERVAL_SECONDS", "").strip()
    poll_min = os.getenv("POLL_INTERVAL_MINUTES", "").strip()
    if poll_sec:
        try:
            poll_interval_seconds = max(0.5, float(poll_sec))
        except ValueError:
            poll_interval_seconds = 0.5
    elif poll_min.isdigit():
        poll_interval_seconds = max(0.5, float(poll_min) * 60)
    else:
        poll_interval_seconds = 0.5
    report_sec = os.getenv("POLL_REPORT_SECONDS", "60").strip()
    try:
        poll_report_seconds = max(poll_interval_seconds, float(report_sec)) if report_sec else 60.0
    except ValueError:
        poll_report_seconds = 60.0
    data = ensure_data_dir(_resolve_data_dir())

    flag_wait = os.getenv("DROP_FLAG_WAIT_SECONDS", "120").strip()
    delay_min = os.getenv("DROP_SNIPE_DELAY_MS_MIN", "50").strip()
    delay_max = os.getenv("DROP_SNIPE_DELAY_MS_MAX", "200").strip()

    drop = DropMonitorSettings(
        user_token=(os.getenv("DROP_USER_TOKEN") or "").strip(),
        user_token_fallback=(os.getenv("DROP_USER_TOKEN_FALLBACK") or "").strip(),
        guild_id=_env_int("DROP_GUILD_ID"),
        channel_id=_env_int("DROP_CHANNEL_ID"),
        test_guild_id=_env_int("DROP_TEST_GUILD_ID"),
        test_channel_id=_env_int("DROP_TEST_CHANNEL_ID"),
        role_id=_env_int("DROP_ROLE_ID"),
        flag_wait_seconds=int(flag_wait) if flag_wait.isdigit() else 120,
        snipe_delay_ms_min=int(delay_min) if delay_min.isdigit() else 50,
        snipe_delay_ms_max=int(delay_max) if delay_max.isdigit() else 200,
    )
    promo = PromoMonitorSettings(
        channel_id=_env_int("PROMO_CHANNEL_ID") or 1522635553575534723,
        test_channel_id=_env_int("PROMO_TEST_CHANNEL_ID") or 1547087951912239225,
        role_id=_env_int("PROMO_ROLE_ID"),
    )

    return Settings(
        discord_token=(os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip(),
        cookie=(os.getenv("COOKIE") or "").strip(),
        discord_guild_id=int(guild) if guild.isdigit() else None,
        watch_channel_id=int(watch) if watch.isdigit() else None,
        poll_interval_seconds=poll_interval_seconds,
        poll_report_seconds=poll_report_seconds,
        debug=os.getenv("DEBUG", "").strip().lower() in {"1", "true", "yes", "on"},
        wallet_owner_id=int(owner) if owner.isdigit() else 1521237044746125462,
        data_dir=data,
        catalog_path=data / "catalog.json",
        catalog_meta_path=data / "catalog-meta.json",
        details_path=data / "details-cache.json",
        ignore_path=data / "ignore-keywords.json",
        drop=drop,
        promo=promo,
        safebuy=os.getenv("SAFEBUY", "").strip().lower() in {"1", "true", "yes", "on"},
    )
