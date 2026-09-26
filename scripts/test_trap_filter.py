"""Extensive trap_text regression: real limiteds must pass, dont/no-buy traps must block."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog.filter import trap_text

CLEAN: list[str] = [
    "Red Domino Crown",
    "Red Domino Crown\nDeathless Glory.",
    "Emerald Valkyrie\nEmerald light, guide my flight.",
    (
        'Red Tango\n"Red Tango" is actually a code name, and you are actually a spy '
        "for Her Majesty's Secret Service. Your cover is that you are an amazing Tango dancer."
    ),
    'Glowing Gold Gift of Superuser\nsudo "manage the community.sh"',
    (
        "https://madxka.com/catalog/18769/Glowing-Gold-Gift-of-Superuser has opened, "
        "inside you find https://madxka.com/catalog/46050/Red-Domino-Crown"
    ),
    "Sparkle Time Fedora",
    "Dominus Empyreus",
    "Dominus Frigidus",
    "Dominus Astra",
    "Beautiful Hair for Beautiful People",
    "Bunny Ears",
    "Rainbow Shaggy",
    "Valkyrie Helm",
    "Clockwork's Shades",
    "The Classic ROBLOX Fedora",
    "Black Iron Domino Crown",
    "Gold Domino Crown",
    "Purple Banded Top Hat",
    "Red Banded Top Hat",
    "Urban Streetwear Cap",
    "Abandoned Warehouse Set",
    "Banana Suit",
    "Bandana",
    "Green Banded Top Hat",
    "Nobody's Business Cap",
    "and you are actually a spy",
    "inside you find a crown",
    "you find",
    "and you",
    "nobody knows",
    "buy me a coffee",
    "purchase history",
    "Stock: 17 | Red Tango",
    "Price: 125 | Stock: 17",
    "no way this is limited",
    "no free lunch here",
    "find your way",
    "sudo manage the community",
]

MUST_BLOCK: list[str] = [
    "DONT BUY",
    "DON'T BUY",
    "dont buy",
    "Don't Buy This",
    "DO NOT BUY",
    "do not buy",
    "DO NOT BUY THIS",
    "NO BUY",
    "no buy",
    "NoBuy",
    "NOBUY",
    "n0 buy",
    "n0 BUY",
    "NOO BUY",
    "D0NT BUY",
    "D0N'T BUY",
    "DN'T BUY",
    "DOPNT BUIY",
    "Dominus Pittacium DOPNT BUIY",
    "DNT BUY",
    "dontbuy",
    "DONOTBUY",
    "DONOT BUY",
    "DO NOTT BUY",
    "DONT BUUY",
    "DONT BUIY",
    "D0NT BU1Y",
    "D0PNT BU1Y",
    "DNNT BUY",
    "DONTT BUY",
    "DNT BUUY",
    "do not purchase",
    "don't purchase",
    "DONT PURCHASE",
    "dontpurchase",
    "DONT PURCHSE",
    "DONT PURCHAS",
    "BUY BAN",
    "buy = ban",
    "buy this = ban",
    "BUY THIS=BAN",
    "you will get banned",
    "if you buy you will be banned",
    "get banned",
    "will be banned",
    "buy and you will be banned",
    "catching limited snipers",
    "catch item snipers",
    "item sniper",
    "item snipers",
    "item snipe",
    "item snipe test",
    "snipe test",
    "catching snipers",
    "sniping items",
    "this is to catch item snipers",
    "you will get banned if you buy",
    "buyban",
    "BUYBANNED",
]


def main() -> int:
    clean_fail: list[tuple[str, str]] = []
    block_fail: list[str] = []

    print("=== CLEAN (expect None) ===")
    for s in CLEAN:
        r = trap_text(s)
        ok = r is None
        print(f"{'OK' if ok else 'FAIL':4} | {r!r:40} | {s[:72]!r}")
        if not ok:
            clean_fail.append((s[:72], r or ""))

    print()
    print("=== MUST BLOCK (expect hit) ===")
    for s in MUST_BLOCK:
        r = trap_text(s)
        ok = r is not None
        print(f"{'OK' if ok else 'FAIL':4} | {r!r:40} | {s[:72]!r}")
        if not ok:
            block_fail.append(s)

    print()
    print("SUMMARY")
    print(f"clean false positives: {len(clean_fail)} / {len(CLEAN)}")
    for s, r in clean_fail:
        print(f"  FP: {s!r} -> {r}")
    print(f"must-block misses: {len(block_fail)} / {len(MUST_BLOCK)}")
    for s in block_fail:
        print(f"  MISS: {s!r}")

    if clean_fail or block_fail:
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
