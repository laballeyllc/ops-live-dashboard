#!/usr/bin/env python3
"""
Product-level stock and demand pull: usable on-hand stock (from the
"Stock Quantity by Sublocation In Units" Finale report, filtered to
real pickable locations only — see is_usable_sublocation() in
ops_common.py for the full derivation) plus backorder demand and
reorder point max (from the "Backordered sales by order (SkD)" report).

Powers the "Chemicals to prioritize" build-planning features on the
dashboard — this is Finale-only, no ShipStation calls at all, so it's
independent of pull_live_queue.py and pull_ops_data.py and can safely
run on its own schedule without affecting either.

Usage:
    python pull_stock_levels.py                  # writes to Supabase
    python pull_stock_levels.py --no-supabase     # dry run, prints only

This script replaces the ENTIRE contents of Supabase's stock_levels
table on every run (not an append) — it represents "right now," not
history, same as live_queue.
"""
import time
import argparse
from datetime import datetime, timezone

from ops_common import build_stock_levels, replace_stock_levels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-supabase", action="store_true",
                         help="Skip writing to Supabase; just fetch and print a summary.")
    args = parser.parse_args()

    run_start = time.time()
    # Always explicit UTC with an explicit offset — same reasoning as
    # the other two pull scripts: correct and consistent whether this
    # runs locally (Central Time) or on GitHub Actions (UTC).
    pulled_at = datetime.now(timezone.utc).isoformat()

    rows = build_stock_levels()
    print(f"Built {len(rows)} product stock/demand rows.")

    if args.no_supabase:
        print("(--no-supabase: skipping Supabase write)")
        for row in rows[:10]:
            print(row)
        if len(rows) > 10:
            print(f"... and {len(rows) - 10} more")
    else:
        replace_stock_levels(rows, pulled_at)

    total = time.time() - run_start
    print(f"Total run time: {total:.1f}s")


if __name__ == "__main__":
    main()
