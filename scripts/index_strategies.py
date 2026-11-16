"""Index the strategy cards in docs/strategies/ into the strategy_cards table.

    python scripts/index_strategies.py            # parse, validate, embed, replace
    python scripts/index_strategies.py --check    # parse and validate only

Needs the database migrated to 0007 and the bge-small weights. Re-running is
safe: the collection is replaced as a whole.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.strategy_cards import load_cards  # noqa: E402


async def main(check: bool) -> int:
    cards = load_cards()
    kinds = {k: sum(c.kind == k for c in cards) for k in ("equity", "option")}
    print(f"{len(cards)} cards valid: {kinds['equity']} equity, {kinds['option']} option")
    if check:
        return 0
    from db.base import engine
    from rag.strategy_cards import index_cards

    started = time.perf_counter()
    try:
        async with engine.begin() as conn:
            rows = await index_cards(conn, cards)
    finally:
        await engine.dispose()
    print(f"indexed {rows} sections in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate only, no database")
    sys.exit(asyncio.run(main(ap.parse_args().check)))
