#!/usr/bin/env python3
"""
Daily inventory turnover pull — two steps:
  1. Logs one row per product to the append-only turnover_snapshots
     table (never replaced, only ever added to) — the real history.
     Deliberately separate from pull_stock_levels.py: that pull
     represents "right now" and is fully replaced every run, while this
     is a daily history log, same relationship health_snapshots has to
     the live queue pull.
  2. Computes the real turnover ratio from that history (using however
     many days have actually elapsed, not a fixed 90-day assumption)
     and writes it to turnover_computed, which the dashboard reads
     directly — kept separate from pull_stock_levels.py and
     pull_ops_data.py since this is its own independent concern with
     its own Finale report pull (Product sales history), not shared
     with either of those pipelines.

Usage:
    python pull_turnover_snapshot.py
"""
from datetime import datetime, timezone
from dotenv import load_dotenv
from ops_common import log_turnover_snapshot, compute_turnover, write_turnover_computed

load_dotenv()


def main():
    pulled_at_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pulled_at_instant = datetime.now(timezone.utc).isoformat()

    print(f"Logging turnover snapshot for {pulled_at_date}...")
    log_turnover_snapshot(pulled_at_date)

    print("\nComputing turnover from accumulated history...")
    results = compute_turnover(pulled_at_instant)
    write_turnover_computed(results)

    print("Done.")


if __name__ == "__main__":
    main()
