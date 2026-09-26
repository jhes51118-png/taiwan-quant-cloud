"""Task Scheduler entry point. Run after 21:30 Asia/Taipei on trading days."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from twquant.cli import main


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The official full-market snapshots use far fewer requests than per-stock history.
    commands = [
        ["universe", "--include-delisted"],
        ["official"],
        ["official-feeds"],
        ["sync", "--start", (date.today() - timedelta(days=400)).isoformat(),
         "--datasets", "revenue", "financials", "balance", "institutions", "margin", "actions",
         "--max-requests", "250"],
    ]
    errors = 0
    for argv in commands:
        logging.info("Running %s", argv[0])
        errors += main(argv) != 0
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(run())
