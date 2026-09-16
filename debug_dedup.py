#!/usr/bin/env python3
"""
Diagnostic only — checks whether today's shipments genuinely map 1:1 to
unique orders, or whether something is silently collapsing/duplicating them.

Usage:
    python debug_dedup.py
"""
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from pull_stats import ss_datetime, WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME

load_dotenv()
client = ShipStationClient()

TZ = ZoneInfo("America/Chicago")
now = datetime.now(TZ)
day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

shipments = client.list_shipments(
    ship_date_start=ss_datetime(day_start),
    ship_date_end=ss_datetime(now),
)

print(f"Total shipments today so far: {len(shipments)}")

order_ids = [s.get("orderId") for s in shipments if s.get("orderId")]
order_numbers = [s.get("orderNumber") for s in shipments if s.get("orderNumber")]

print(f"Unique orderId count:     {len(set(order_ids))}  (out of {len(order_ids)} present)")
print(f"Unique orderNumber count: {len(set(order_numbers))}  (out of {len(order_numbers)} present)")

dupe_order_ids = [oid for oid, count in Counter(order_ids).items() if count > 1]
print(f"orderIds appearing MORE THAN ONCE (i.e. multi-package orders): {len(dupe_order_ids)}")
if dupe_order_ids:
    print("Sample duplicated orderIds and their shipment counts:")
    counts = Counter(order_ids)
    for oid in dupe_order_ids[:10]:
        matching = [s for s in shipments if s.get("orderId") == oid]
        print(f"  orderId={oid}  shipmentCount={counts[oid]}  orderNumbers={[s.get('orderNumber') for s in matching]}")

print()
print("Breakdown by warehouseId:")
wh_counts = Counter(s.get("warehouseId") for s in shipments)
for wh_id, count in wh_counts.most_common():
    print(f"  warehouseId={wh_id}: {count} shipments")

warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
print()
print(f"Resolved Warehouse location id: {warehouse_id} ({WAREHOUSE_LOCATION_NAME})")
print(f"Resolved Freight location id:   {freight_id} ({FREIGHT_LOCATION_NAME})")
