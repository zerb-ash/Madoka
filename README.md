# Madoka

Discord bot for [madxka.com](https://madxka.com) catalog monitoring, item inspection, and wallet purchases.

## Setup

1. Copy `.env.example` to `.env` and fill in values.
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python main.py`

Catalog cache ships in `data/` (`catalog.json`, `catalog-meta.json`, `details-cache.json`).

## Commands

- `/inspect` — look up an item by id or name
- `/balance` — wallet robux and tickets (owner only)
- `/buy free` — purchase all free on-sale catalog items (owner only)
- `/redeem existing` — parse promo channel history and redeem every found code (owner only)
- `/catalog stats` / `/catalog refresh` — cache info and force sync
- `/test random` — random item inspect

## Config

| Variable | Description |
|----------|-------------|
| `TOKEN` | Discord bot token |
| `COOKIE` | madxka session cookie (JWT or full browser cookie string) |
| `DISCORD_GUILD_ID` | Guild for slash command sync |
| `WATCH_CHANNEL_ID` | Channel for catalog poll embeds |
| `WALLET_OWNER_ID` | Discord user id allowed to use wallet commands |
| `POLL_INTERVAL_SECONDS` | Catalog poll interval in seconds (default 0.5 → 120/min) |
| `POLL_REPORT_SECONDS` | Minute report interval in seconds (default 60) |
| `SAFEBUY` | `true` = during channel buy delay, poll sales and wait for ≥3 (skip serial #1-3); still buys if stock rushes or times out |
| `DATA_DIR` | Cache directory (default `./data`). On Railway use a volume path like `/data` |
| `PROMO_CHANNEL_ID` | Official promocode channel to snipe |
| `PROMO_TEST_CHANNEL_ID` | Test promocode channel (no role ping required) |
| `PROMO_ROLE_ID` | Optional `@Code Pings` role id; when set, official channel requires that ping |

## Railway

1. Deploy from this repo. Do **not** set a custom build command — Railpack installs deps from `requirements.txt` automatically.
2. Start command: `python main.py` (set in `railway.json` / `Procfile`).
3. Set env vars from `.env.example` (`TOKEN`, `COOKIE`, guild/channel ids, etc.).
4. **Persist cache across deploys:** create a volume mounted at `/data`, then set `DATA_DIR=/data`.
5. On first boot with an empty volume, the bot copies the bundled `data/` cache into `DATA_DIR`, then keeps writing updates there with atomic saves.

If deploy fails with `python main.py` during **build**, remove any custom `buildCommand` in the Railway service settings and redeploy.
