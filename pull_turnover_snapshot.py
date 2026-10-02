#!/usr/bin/env python3
"""
Daily inventory turnover snapshot — logs one row per product to the
append-only turnover_snapshots table (never replaced, only ever added
to), what turnover will eventually be computed from once enough daily
history accumulates. Deliberately separate from pull_stock_levels.py:
that pull represents "right now" and is fully replaced every run, while
this is a daily history log, same relationship health_snapshots has to
the live queue pull.

Usage:
    python pull_turnover_snapshot.py
"""
from datetime import datetime, timezone
from dotenv import load_dotenv
from ops_common import log_turnover_snapshot

load_dotenv()


def main():
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Logging turnover snapshot for {pulled_at}...")
    log_turnover_snapshot(pulled_at)
    print("Done.")


if __name__ == "__main__":
    main()
