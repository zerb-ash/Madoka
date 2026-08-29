from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.client import MadokaBot
from config.settings import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        raise SystemExit("Set TOKEN in .env")
    if not settings.cookie:
        raise SystemExit("Set COOKIE in .env")
    if settings.debug:
        if settings.watch_channel_id:
            print(f"watch channel {settings.watch_channel_id}")
        print(f"data dir {settings.data_dir}")
    bot = MadokaBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
