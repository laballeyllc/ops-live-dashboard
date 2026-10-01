#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Checks,
directly against LIVE ShipStation data (not any historical table), how
much of the CURRENT Warehouse/Freight queue was placed before a given
cutoff date — the cleanest way to check whether "more Out than In every
week" is explained by a real pre-existing backlog, or points to a
genuine counting bug.

Usage:
    python debug_queue_age_breakdown.py
"""
import json
from collections import Counter
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from ops_common import WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME

load_dotenv()

CUTOFF_DATE = "2026-08-31"  # the start of Casey's earliest measured week

client = ShipStationClient()
warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
valid_ids = {warehouse_id, freight_id}

print(f"Expected warehouse_id={warehouse_id}, freight_id={freight_id}")
print(f"Checking current queue against cutoff: {CUTOFF_DATE}\n")

# Pull the CURRENT, live queue directly: awaiting_shipment + on_hold,
# no date filtering at all (we want everything, regardless of age).
all_queue_orders = []
for status in ("awaiting_shipment", "on_hold"):
    page = 1
    while True:
        data = client._get("/orders", {"orderStatus": status, "page": page, "pageSize": 500})
        batch = data.get("orders", [])
        all_queue_orders.extend(batch)
        if page >= data.get("pages", 1):
            break
        page += 1

scoped = [o for o in all_queue_orders if (o.get("advancedOptions") or {}).get("warehouseId") in valid_ids]
print(f"Total current Warehouse/Freight queue (awaiting_shipment + on_hold): {len(scoped)}")

# Split by whether the order predates our measurement window.
old_orders = [o for o in scoped if (o.get("orderDate") or "")[:10] < CUTOFF_DATE]
recent_orders = [o for o in scoped if (o.get("orderDate") or "")[:10] >= CUTOFF_DATE]

print(f"...placed BEFORE {CUTOFF_DATE} (pre-existing backlog): {len(old_orders)}")
print(f"...placed ON OR AFTER {CUTOFF_DATE} (within measured weeks): {len(recent_orders)}")

if old_orders:
    print(f"\n=== Age distribution of the {len(old_orders)} pre-existing backlog orders ===")
    year_month_counts = Counter((o.get("orderDate") or "")[:7] for o in old_orders)
    for ym, count in sorted(year_month_counts.items()):
        print(f"  {ym}: {count}")

    print("\n=== Sample of 3 oldest backlog orders ===")
    sorted_old = sorted(old_orders, key=lambda o: o.get("orderDate") or "")
    for o in sorted_old[:3]:
        print(f"  Order {o.get('orderNumber')}: placed {o.get('orderDate')}, status {o.get('orderStatus')}, "
              f"modified {o.get('modifyDate')}")

# New check: does a wide modifyDate-based fetch of "shipped" orders
# (the exact query the Orders Out function runs) contain DUPLICATE
# orderIds? Page-number pagination against a live, constantly-changing
# dataset (orders shipping WHILE we paginate) is a known way to get
# duplicate or skipped rows -- this checks directly whether that's
# happening here.
print("\n\n=== Checking for duplicate orders in a real Orders-Out-style fetch ===")
start_date, end_date = "2026-08-31", "2026-09-04"  # Casey's FIRST measured week — the right one to test backlog against, since any pre-existing backlog effect should be concentrated right at the start of the measured period, not the third week
modify_start = "2026-08-28 00:00:00"  # start_date - 3 days
modify_end = "2026-09-07 23:59:59"    # end_date + 3 days

shipped_orders = []
page = 1
while True:
    data = client._get("/orders", {
        "orderStatus": "shipped",
        "modifyDateStart": modify_start,
        "modifyDateEnd": modify_end,
        "page": page,
        "pageSize": 500,
    })
    batch = data.get("orders", [])
    shipped_orders.extend(batch)
    print(f"  page {page}/{data.get('pages', 1)} ({len(shipped_orders)} so far)")
    if page >= data.get("pages", 1):
        break
    page += 1

order_ids = [o.get("orderId") for o in shipped_orders]
id_counts = Counter(order_ids)
duplicates = {oid: count for oid, count in id_counts.items() if count > 1}

print(f"\nTotal rows fetched: {len(shipped_orders)}")
print(f"Distinct orderIds: {len(id_counts)}")
print(f"Duplicate orderIds found: {len(duplicates)}")
if duplicates:
    print(f"Total duplicate ROWS (excess beyond first occurrence): {sum(c - 1 for c in duplicates.values())}")
    sample_dup_id = list(duplicates.keys())[0]
    print(f"\nSample duplicated order (orderId={sample_dup_id}, appeared {duplicates[sample_dup_id]} times)")

# Now apply the SAME filtering the real function does, with and without
# de-duplication, to see the actual impact on the reported count.
scoped_shipped = [o for o in shipped_orders if (o.get("advancedOptions") or {}).get("warehouseId") in valid_ids]
in_range = [o for o in scoped_shipped if start_date <= (o.get("shipDate") or "")[:10] <= end_date]
distinct_in_range = {o.get("orderId"): o for o in in_range}

print(f"\n=== Applying full Orders Out filter for {start_date} to {end_date} ===")
print(f"Without de-dup (what the current function reports): {len(in_range)}")
print(f"With de-dup by orderId: {len(distinct_in_range)}")

# The check I missed last time: of the orders that actually SHIPPED in
# this window (counted in "Out"), how many were placed BEFORE 8/31 --
# i.e. real old backlog that finished shipping during these weeks, and
# so would be invisible to a "what's still sitting unshipped" check,
# but would still inflate Out without a matching In for this window.
placed_before_cutoff = [o for o in in_range if (o.get("orderDate") or "")[:10] < "2026-08-31"]
placed_within_period = [o for o in in_range if (o.get("orderDate") or "")[:10] >= "2026-08-31"]
print(f"\nOf the {len(in_range)} orders that SHIPPED in this window:")
print(f"  ...placed BEFORE 2026-08-31 (old backlog finishing up): {len(placed_before_cutoff)}")
print(f"  ...placed on/after 2026-08-31 (within the measured period): {len(placed_within_period)}")
