from __future__ import annotations

"""
Probe madxka details API rate limits.

Usage:
  python scripts/test_details_ratelimit.py
  python scripts/test_details_ratelimit.py --interval 0.25 --count 40 --id 39113
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from madxka.http import MadxkaApiError, MadxkaHttp


async def run_probe(*, item_id: int, interval: float, count: int) -> None:
    settings = load_settings()
    http = MadxkaHttp(settings.cookie)
    entry = [{"id": int(item_id), "itemType": "Asset"}]

    ok = 0
    fail = 0
    statuses: dict[int, int] = {}
    latencies: list[float] = []
    first_fail: str | None = None

    print(f"probing details for id={item_id} · interval={interval}s · count={count}")
    started = time.perf_counter()
    for i in range(1, count + 1):
        t0 = time.perf_counter()
        try:
            rows = await http.item_details(entry)
            ms = (time.perf_counter() - t0) * 1000
            latencies.append(ms)
            ok += 1
            sale = rows[0].get("saleCount") if rows else None
            serial = rows[0].get("serialCount") if rows else None
            print(f"[{i}/{count}] ok {ms:.0f}ms · sales={sale} serials={serial}")
        except MadxkaApiError as e:
            fail += 1
            statuses[e.status] = statuses.get(e.status, 0) + 1
            if first_fail is None:
                first_fail = str(e)[:200]
            print(f"[{i}/{count}] FAIL {e.status}")
        except Exception as e:
            fail += 1
            if first_fail is None:
                first_fail = str(e)[:200]
            print(f"[{i}/{count}] FAIL {e}")

        if i < count and interval > 0:
            await asyncio.sleep(interval)

    elapsed = time.perf_counter() - started
    await http.close()

    print("\n=== summary ===")
    print(f"elapsed {elapsed:.2f}s · ok={ok} fail={fail} · rps={count/elapsed:.2f}")
    if latencies:
        print(
            f"latency ms · min={min(latencies):.0f} "
            f"avg={sum(latencies)/len(latencies):.0f} max={max(latencies):.0f}"
        )
    if statuses:
        print(f"status counts {statuses}")
    if first_fail:
        print(f"first fail: {first_fail}")
    if fail == 0:
        print(f"interval {interval}s looks safe for this sample")
    else:
        print(f"interval {interval}s hit errors — back off")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, default=39113)
    parser.add_argument("--interval", type=float, default=0.25, help="seconds between requests")
    parser.add_argument("--count", type=int, default=40)
    args = parser.parse_args()
    asyncio.run(run_probe(item_id=args.id, interval=args.interval, count=args.count))


if __name__ == "__main__":
    main()
