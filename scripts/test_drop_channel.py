from __future__ import annotations

"""
Standalone test: verify a Discord user token can read drop / test channels.

Usage:
  python scripts/test_drop_channel.py
  python scripts/test_drop_channel.py --live
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from drop.parser import parse_drop_message
from drop.user_monitor import DropUserMonitor


async def dump_channel(monitor: DropUserMonitor, channel_id: int, label: str, role_id: int | None, *, is_test: bool = False) -> None:
    print(f"\n=== {label} ({channel_id}) ===")
    try:
        rows = await monitor.fetch_recent(channel_id, limit=15)
    except Exception as e:
        print(f"FAILED to read channel: {e}")
        return
    print(f"fetched {len(rows)} message(s)")
    for row in rows:
        content = str(row.get("content") or "")
        embeds = row.get("embeds") if isinstance(row.get("embeds"), list) else []
        msg_id = int(row.get("id") or 0)
        author = (row.get("author") or {}).get("username")
        drop = parse_drop_message(
            content=content,
            embeds=embeds,
            channel_id=channel_id,
            message_id=msg_id,
            role_id=role_id,
            is_test=is_test,
        )
        preview = (content or "(embed-only)").replace("\n", " | ")[:160]
        print(
            f"- {msg_id} · {author} · drop={drop.is_drop} "
            f"ids={list(drop.item_ids)} ping={drop.has_ping} test={drop.is_test}"
        )
        try:
            print(f"  {preview}")
        except UnicodeEncodeError:
            print(f"  {preview.encode('ascii', 'replace').decode('ascii')}")


async def live_listen(monitor: DropUserMonitor) -> None:
    print("\nListening for new messages (Ctrl+C to stop)…")

    async def on_drop(drop):
        print(
            f"[live] ch={drop.channel_id} msg={drop.message_id} "
            f"drop={drop.is_drop} ids={list(drop.item_ids)} ping={drop.has_ping}"
        )
        if drop.content:
            print(f"  {drop.content[:200].replace(chr(10), ' | ')}")

    monitor.on_drop = on_drop
    monitor.start()
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await monitor.stop()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Also listen for new messages")
    args = parser.parse_args()

    settings = load_settings()
    drop = settings.drop
    tokens = [t for t in (drop.user_token, drop.user_token_fallback) if t]
    if not tokens:
        raise SystemExit("Set DROP_USER_TOKEN in .env")

    channels = drop.watched_channel_ids()
    if not channels:
        raise SystemExit("Set DROP_CHANNEL_ID and/or DROP_TEST_CHANNEL_ID")

    monitor = None
    last_err = None
    test_ids = {drop.test_channel_id} if drop.test_channel_id else set()
    for i, token in enumerate(tokens, start=1):
        candidate = DropUserMonitor(
            token,
            channel_ids=channels,
            test_channel_ids=test_ids,
            role_id=drop.role_id,
            debug=True,
        )
        try:
            me = await candidate.probe_auth()
            print(f"auth ok token#{i} · {me.get('username')} ({me.get('id')})")
            monitor = candidate
            break
        except Exception as e:
            last_err = e
            print(f"token#{i} failed: {e}")
            await candidate.stop()

    if monitor is None:
        raise SystemExit(f"No working user token ({last_err})")

    # REST probe first
    if drop.channel_id:
        await dump_channel(monitor, drop.channel_id, "LIVE drop channel", drop.role_id, is_test=False)
    if drop.test_channel_id:
        await dump_channel(monitor, drop.test_channel_id, "TEST drop channel", drop.role_id, is_test=True)

    if args.live:
        try:
            await live_listen(monitor)
        except KeyboardInterrupt:
            print("stopped")
    else:
        await monitor.stop()
        print("\nDone. Re-run with --live to watch new messages.")


if __name__ == "__main__":
    asyncio.run(main())
