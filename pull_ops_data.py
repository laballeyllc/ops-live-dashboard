#!/usr/bin/env python3
"""
ShipStation is the source of truth for "what's shipped, what's still in
queue, and which fulfillment queue it's routed to." Finale is used for
exactly one thing: given an Order ID, what material(s) were on it and
what are their Category / CHEM / CHEM TECH / LAB ROOM attributes.

Why this doesn't classify Warehouse vs. Freight from a field like Ship
From Location anymore: that's not actually how ShipStation routes orders.
Routing is driven by a real set of Tags applied by automation rules —
Warehouse, "Jerry" (= Freight), plus several drop-ship vendors (Biologix,
United Scientific, Transene, GT), person-assignments, and holds (Fraud
Risk, Pending Approval, etc.). Trying to infer that from
advancedOptions.warehouseId was reverse-engineering a system we could
just read directly — so now we do: pull each order's real Tags from
ShipStation and report them as-is, rather than trying to recompute the
routing logic ourselves.

Why the previous approach (joining Finale's Orders report to Finale's
Shipments report) got replaced with ShipStation entirely for status:
Finale's own Shipments report turned out to be unreliable for status.
Split/backordered orders get compound Order IDs on the Orders side (e.g.
"000382083-1A") that don't consistently match the plain Order ID on the
Shipments side, which made ~1,100 genuinely-shipped orders look like they
were still sitting in queue.

Data flow:
  1. Pull the Finale Orders report ("Ops Live Dashboard") — used ONLY for
     product attributes, grouped by Order ID. Not used for status at all.
  2. Pull ShipStation's tag list (tagId -> name), so we can translate the
     numeric tagIds on each order into real names.
  3. Pull ShipStation's current queue directly: every order with status
     awaiting_shipment or on_hold, with NO date restriction — a stuck
     order could be weeks old and still needs to show up as "in queue."
  4. Pull ShipStation's actual Shipment records (not Finale's) for the
     recent window (--days) for ship date / voided-or-not, plus the
     matching Order records (tags aren't on the shipment record itself)
     for that same window, so shipped orders get tags too.
  5. For each unique Order ID from steps 3+4, look up its Finale product
     line(s) by Order ID and fan out one row per product line.

Usage:
    python pull_ops_data.py                  # last 30 days of shipments;
                                              # the queue pull (step 3) is
                                              # always full, regardless of
                                              # this flag — a queue item
                                              # doesn't stop being "in
                                              # queue" just because it's
                                              # been sitting a while.
    python pull_ops_data.py --days 7
    python pull_ops_data.py --days 0          # shipments: full history
    python pull_ops_data.py --no-history      # CSV only, skip the database
                                              # (useful for quick test runs)

Output:
    ops_data.csv — a local convenience copy of this run's rows, for
                   quick spot-checks. Overwritten every run, on purpose.
    Supabase 'snapshots' table — the real historical record. Every run
                   APPENDS its rows here, tagged with a pulled_at
                   timestamp; nothing is ever overwritten or deleted.
                   Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
                   (see .env / GitHub Secrets) unless run with
                   --no-supabase.
"""
import os
import csv
import time
import argparse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from shipstation_client import ShipStationClient
from pull_stats import ss_datetime
from ops_common import (
    normalize_order_id, tags_for_order, core_queue_for_order, ss_items_for_order,
    order_weight_lbs, order_item_quantity, store_name_for_order,
    fetch_finale_product_lines, append_to_snapshots,
    WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME,
)

load_dotenv()


def pull_shipstation_state(client: ShipStationClient, days: int) -> dict[str, dict]:
    """Returns dict: Order ID -> {Order date, Shipment status,
    Ship date actual, Shipment ID, Tags, Core Queue}. This is
    ShipStation's view of the world, full stop — nothing here comes
    from Finale."""
    print("Pulling tag list from ShipStation...")
    tag_name_by_id = client.list_tags()
    print(f"  {len(tag_name_by_id)} tags defined")

    print("Pulling store list from ShipStation...")
    store_name_by_id = client.list_stores()
    print(f"  {len(store_name_by_id)} stores defined")

    warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)

    state: dict[str, dict] = {}

    # Step 1: current queue, no date limit. Tags and Ship From Location
    # are on the order object directly here, so no extra lookup needed.
    #
    # IMPORTANT: on_hold orders ARE pulled here (for the Held/Compliance
    # panel), but must NEVER be counted in the main Warehouse/Freight
    # metrics — that exclusion lives in the FRONTEND now (buildSummary()
    # filters out shipment_status === "on_hold" before computing
    # anything Warehouse/Freight-related). See pull_live_queue.py's
    # matching function for the full history of why this matters (order
    # 220199 already caused a real bug here once).
    print("Pulling current queue from ShipStation (awaiting_shipment + on_hold, no date limit)...")
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
                "Ship date actual": "",
                "Shipment ID": "",
                "Tags": tags_for_order(order, tag_name_by_id),
                "Core Queue": core_queue_for_order(order, warehouse_id, freight_id),
                "SS Items": ss_items_for_order(order),
                "Weight (lbs)": order_weight_lbs(order),
                "Item Quantity": order_item_quantity(order),
                "Store": store_name_for_order(order, store_name_by_id),
            }

    # Step 2: actual shipments for the recent window — the authoritative
    # ship date and voided-or-not. Tags AND Ship From Location both live
    # on the ORDER, not the shipment record, so we also bulk-fetch orders
    # modified in this same window to pick up both for anything shipped.
    if days > 0:
        window_start = datetime.now() - timedelta(days=days)
        window_end = datetime.now()
        print(f"Pulling shipments from ShipStation ({window_start.date()} to {window_end.date()})...")
        shipments = client.list_shipments(
            ss_datetime(window_start), ss_datetime(window_end), include_voided=True
        )
        print(f"  {len(shipments)} shipments")

        print("Pulling matching orders (for Tags and Ship From Location)...")
        recent_orders = client.list_orders(
            modify_date_start=ss_datetime(window_start - timedelta(days=3)),
            modify_date_end=ss_datetime(window_end + timedelta(days=1)),
        )
        print(f"  {len(recent_orders)} orders")
        order_by_number = {normalize_order_id(o.get("orderNumber")): o for o in recent_orders}

        for shipment in shipments:
            oid = normalize_order_id(shipment.get("orderNumber"))
            if not oid:
                continue
            order = order_by_number.get(oid)
            state[oid] = {
                "Order date": (shipment.get("createDate") or "")[:10],
                "Shipment status": "voided" if shipment.get("voided") else "shipped",
                "Ship date actual": (shipment.get("shipDate") or "")[:10],
                "Shipment ID": str(shipment.get("shipmentId", "")),
                "Tags": tags_for_order(order, tag_name_by_id) if order else "",
                "Core Queue": core_queue_for_order(order, warehouse_id, freight_id) if order else "",
                "SS Items": ss_items_for_order(order) if order else "[]",
                "Weight (lbs)": order_weight_lbs(order) if order else 0.0,
                "Item Quantity": order_item_quantity(order) if order else 0,
                "Store": store_name_for_order(order, store_name_by_id) if order else "",
            }

    return state


def build_rows(finale_lines_by_order: dict[str, list[dict]], ss_state: dict[str, dict]) -> list[dict]:
    """One row per (ShipStation order x Finale product line). Orders with
    ZERO matching Finale product lines are skipped entirely — confirmed
    directly against a real mixed order (000323837) that this means the
    order is fully drop-ship: those line items never get recorded in
    Finale at all (the vendor ships them directly), so there's nothing
    Lab Alley actually fulfilled on that order. An order WITH Finale
    lines already contains only its real Warehouse/Freight-fulfilled
    lines — Finale does that line-level split for us; we don't need to
    reproduce it, just avoid inserting a blank placeholder for the
    zero-match case, which is what used to leak drop-ship-only orders in
    as blank rows."""
    rows = []
    for oid, info in ss_state.items():
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
                "Shipment ID": info["Shipment ID"],
                "Ship date actual": info["Ship date actual"],
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
    # utf-8-sig: Windows' default file encoding can't handle characters
    # like ≥ that show up in chemical/product descriptions, and Excel
    # needs the BOM to read UTF-8 CSVs correctly.
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        # Almost always means the file is open in Excel (or similar) and
        # Windows has it locked. Rather than lose the whole run's output,
        # fall back to a timestamped filename so nothing's lost — just
        # close the original file in Excel before the next run.
        fallback = path.rsplit(".", 1)[0] + f"_{datetime.now():%Y%m%d_%H%M%S}.csv"
        print(f"'{path}' is locked (probably open in Excel) — "
              f"writing to '{fallback}' instead. Close the original file "
              f"in Excel before the next run to avoid this.")
        with open(fallback, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        path = fallback
    print(f"Wrote {len(rows)} rows to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=30,
        help="How many days of SHIPPED history to include (0 = full history). "
             "The current queue is always pulled in full regardless of this.",
    )
    parser.add_argument("--out", default="ops_data.csv", help="Local CSV path — a convenience copy for spot-checking a run; Supabase is the real store.")
    parser.add_argument("--no-supabase", action="store_true", help="Skip writing to Supabase (local CSV only — useful for testing without touching the real database).")
    args = parser.parse_args()

    run_start = time.time()
    # Always explicit UTC with an explicit offset (+00:00), never a bare
    # local-time string. This script runs both on Casey's own machine
    # (Central Time) and on GitHub Actions runners (UTC) — datetime.now()
    # with no timezone attached silently picks up whichever clock it
    # happens to run on, which made timestamps inconsistent and looked
    # like they were "in the future" when the two got compared. Postgres'
    # timestamptz column needs this explicit offset to store the right
    # absolute instant regardless of which machine sent it.
    pulled_at = datetime.now(timezone.utc).isoformat()

    client = ShipStationClient()
    t0 = time.time()
    ss_state = pull_shipstation_state(client, args.days)
    print(f"Total unique orders from ShipStation: {len(ss_state)}  ({time.time()-t0:.1f}s)")

    t0 = time.time()
    finale_lines_by_order = fetch_finale_product_lines()
    print(f"Finale pull took {time.time()-t0:.1f}s")

    rows = build_rows(finale_lines_by_order, ss_state)
    write_csv(rows, args.out)

    if not args.no_supabase:
        append_to_snapshots(rows, pulled_at)

    print(f"Total run time: {time.time()-run_start:.1f}s")


if __name__ == "__main__":
    main()
