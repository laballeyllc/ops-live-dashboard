#!/usr/bin/env python3
"""
The "right now" view: same row shape as pull_ops_data.py (full product
breakdown — Category, CHEM, CHEM TECH, LAB ROOM — plus Tags and Core
Queue), but scoped to ONLY the orders currently sitting in ShipStation's
queue (awaiting_shipment + on_hold), not 30 days of shipped history.

Important honesty about what "live" can actually mean here: the
ShipStation side of this pull is small and fast (only ~360 orders right
now). But Finale's Reporting API has no way to ask for "just these N
order IDs" — we still have to pull the ENTIRE Orders report (150k+ rows)
and filter locally to find product info for whichever orders are
currently queued. That Finale pull is the one real bottleneck, and
narrowing the ShipStation side to "queue only" does not make it any
smaller. So this is NOT a 20-second-refresh script — see the timing
printed at the end of each run to find out how often it can actually run
safely. Pick a refresh interval with real margin above that number, not
right up against it.

Usage:
    python pull_live_queue.py                    # writes to Supabase + local CSV
    python pull_live_queue.py --no-supabase       # local CSV only, for testing

This script replaces the ENTIRE contents of Supabase's live_queue table
on every run (not an append) — it represents "right now," not history.
The historical side stays pull_ops_data.py's exclusive job, on its own
schedule, writing to the separate 'snapshots' table.

Note on the lock file below: it protects against overlapping runs on a
single persistent machine (e.g. if this is ever run locally via Task
Scheduler again). It does NOT do anything useful when run on GitHub
Actions, since each scheduled run starts in a fresh, empty container with
no memory of a previous run's lock file — overlap protection there comes
from the workflow YAML's `concurrency:` setting instead. Left in place
since it's harmless either way and still matters for local runs.
"""
import os
import csv
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from shipstation_client import ShipStationClient
from ops_common import (
    normalize_order_id, tags_for_order, core_queue_for_order, ss_items_for_order,
    order_weight_lbs, order_item_quantity, store_name_for_order,
    fetch_finale_product_lines, replace_live_queue,
    WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME,
)

load_dotenv()

# Simple file-based lock so a scheduled run (every 5 min) never starts on
# top of a previous run that's still going — e.g. if Finale is slow one
# day, or a network hiccup stalls a request. Stale locks (older than 15
# min — 3x the scheduled interval, generous enough that it's clearly
# abandoned rather than just slow) are treated as crashed runs and
# cleared automatically, so one bad run can't permanently jam the
# schedule.
LOCK_PATH = "pull_live_queue.lock"
STALE_LOCK_MINUTES = 15


def pull_current_queue(client: ShipStationClient) -> dict[str, dict]:
    """Returns dict: Order ID -> {Order date, Shipment status, Tags, Core
    Queue} for every order currently awaiting_shipment OR on_hold. No
    date limit — a stuck order doesn't stop being relevant just because
    it's old.

    IMPORTANT: on_hold orders ARE pulled here (for the Held/Compliance
    panel, which needs them), but they must NEVER be counted in the main
    Warehouse/Freight queue metrics — that exact bug already happened
    once (order 220199, on_hold, 225 days old, incorrectly inflating
    "oldest in queue"). The exclusion now lives in the FRONTEND instead
    (buildSummary() explicitly filters out shipment_status === "on_hold"
    before computing anything Warehouse/Freight-related), not here. If
    you're reading this because a held order snuck back into the main
    counts, that frontend filter is almost certainly what broke."""
    print("Pulling tag list from ShipStation...")
    tag_name_by_id = client.list_tags()
    print(f"  {len(tag_name_by_id)} tags defined")

    print("Pulling store list from ShipStation...")
    store_name_by_id = client.list_stores()
    print(f"  {len(store_name_by_id)} stores defined")

    warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)

    state: dict[str, dict] = {}
    print("Pulling current queue from ShipStation (awaiting_shipment + on_hold)...")
    for status in ("awaiting_shipment", "on_hold"):
        orders = client.list_orders(order_status=status)
        print(f"  {status}: {len(orders)} orders")
        for order in orders:
            oid = normalize_order_id(order.get("orderNumber"))
            if not oid:
                continue
            state[oid] = {
                "Order date": (order.get("orderDate") or "")[:10],
                "Shipment status": status,
                "Tags": tags_for_order(order, tag_name_by_id),
                "Core Queue": core_queue_for_order(order, warehouse_id, freight_id),
                "SS Items": ss_items_for_order(order),
                "Weight (lbs)": order_weight_lbs(order),
                "Item Quantity": order_item_quantity(order),
                "Store": store_name_for_order(order, store_name_by_id),
            }
    return state


def build_rows(finale_lines_by_order: dict[str, list[dict]], queue_state: dict[str, dict]) -> list[dict]:
    """Same row shape as pull_ops_data.py's historical output (minus
    shipment-specific fields that don't apply to something still in
    queue), so the live view and the historical view are directly
    comparable — just different scopes of the same picture.

    Orders with ZERO matching Finale product lines are skipped entirely
    — not given a placeholder row. Confirmed directly against a real
    mixed order (000323837): drop-ship line items never get recorded in
    Finale at all (the vendor ships them directly; they never enter Lab
    Alley's own fulfillment system), so an order with no Finale lines is
    a fully drop-ship order, and an order WITH Finale lines already
    contains only its real Warehouse/Freight-fulfilled lines — Finale
    itself does the line-level split for us. We don't need to reproduce
    that logic; we just need to stop inserting a placeholder for the
    zero-match case, which is what used to leak drop-ship-only orders
    into the dashboard as blank rows."""
    rows = []
    for oid, info in queue_state.items():
        lines = finale_lines_by_order.get(oid)
        if not lines:
            continue  # fully drop-ship order — no Lab Alley-fulfilled lines at all
        for line in lines:
            rows.append({
                "Order ID": oid,
                "Order date": info["Order date"] or line.get("Order date", ""),
                "Product ID": line.get("Product ID", ""),
                "Description": line.get("Description", ""),
                "Category": line.get("Category", ""),
                "CHEM": line.get("CHEM", ""),
                "CHEM TECH": line.get("CHEM TECH", ""),
                "LAB ROOM": line.get("LAB ROOM", ""),
                "Downpack Product": line.get("Downpack Product", ""),
                "HAZMAT": line.get("HAZMAT", ""),
                "Shipment status": info["Shipment status"],
                "Tags": info["Tags"],
                "Core Queue": info["Core Queue"],
                "SS Items": info["SS Items"],
                "Weight (lbs)": info["Weight (lbs)"],
                "Item Quantity": info["Item Quantity"],
                "Store": info["Store"],
            })
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        print("No rows to write.")
        return
    fieldnames = list(rows[0].keys())
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        fallback = path.rsplit(".", 1)[0] + f"_{datetime.now():%Y%m%d_%H%M%S}.csv"
        print(f"'{path}' is locked (probably open in Excel) — writing to '{fallback}' instead.")
        with open(fallback, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        path = fallback
    print(f"Wrote {len(rows)} rows to {path}")


def acquire_lock(lock_path: str, stale_minutes: int) -> bool:
    """Returns True if we got the lock and should proceed. If a lock file
    already exists but is older than `stale_minutes`, treats it as a
    crashed run, clears it, and proceeds anyway — a single stuck run
    should never permanently jam the 5-minute schedule."""
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < stale_minutes * 60:
            print(
                f"Another run appears to still be in progress "
                f"(lock file is only {age:.0f}s old) — skipping this cycle "
                f"rather than overlapping it."
            )
            return False
        print(
            f"Found a stale lock file ({age/60:.1f} min old, older than "
            f"{stale_minutes} min) — treating the previous run as crashed "
            f"and proceeding."
        )
    with open(lock_path, "w") as f:
        f.write(datetime.now().isoformat())
    return True


def release_lock(lock_path: str) -> None:
    if os.path.exists(lock_path):
        os.remove(lock_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="live_queue.csv", help="Local CSV path — a convenience copy for spot-checking a run; Supabase is the real store.")
    parser.add_argument("--no-supabase", action="store_true", help="Skip writing to Supabase (local CSV only — useful for testing without touching the real database).")
    args = parser.parse_args()

    if not acquire_lock(LOCK_PATH, STALE_LOCK_MINUTES):
        sys.exit(0)  # exit 0, not an error — "skipped this cycle" is expected behavior

    try:
        run_start = time.time()
        # Always explicit UTC with an explicit offset — see the same fix
        # and full explanation in pull_ops_data.py. Runs both locally
        # (Central Time) and on GitHub Actions (UTC); this makes the
        # timestamp correct and consistent regardless of which one ran.
        pulled_at = datetime.now(timezone.utc).isoformat()

        client = ShipStationClient()
        t0 = time.time()
        queue_state = pull_current_queue(client)
        ss_elapsed = time.time() - t0
        print(f"Total unique orders in queue: {len(queue_state)}  ({ss_elapsed:.1f}s)")

        t0 = time.time()
        finale_lines_by_order = fetch_finale_product_lines()
        finale_elapsed = time.time() - t0
        print(f"Finale pull took {finale_elapsed:.1f}s")

        rows = build_rows(finale_lines_by_order, queue_state)
        write_csv(rows, args.out)

        if not args.no_supabase:
            replace_live_queue(rows, pulled_at)

        total = time.time() - run_start
        print(f"Total run time: {total:.1f}s")
        print(
            f"(ShipStation: {ss_elapsed:.1f}s, Finale: {finale_elapsed:.1f}s — "
            f"Finale is the real bottleneck here; use this number to pick a "
            f"safe refresh interval with real margin, not right up against it.)"
        )
    finally:
        release_lock(LOCK_PATH)


if __name__ == "__main__":
    main()
