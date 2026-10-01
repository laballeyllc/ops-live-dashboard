#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Checks
whether ShipStation SHIPMENT records reliably carry a warehouseId field
matching our two known Ship From Location IDs, the same way ORDER
records already do (confirmed earlier this project) — this is the
leading suspect for why "Orders Out" looked severely undercounted.

Usage:
    python debug_shipment_warehouse.py
"""
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from ops_common import WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME

load_dotenv()

client = ShipStationClient()

warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
print(f"Expected warehouse_id={warehouse_id}, freight_id={freight_id}")

# Pull a real, recent window of shipments (last 7 days) to inspect.
end = datetime.now()
start = end - timedelta(days=7)
shipments = client.list_shipments(
    start.strftime("%Y-%m-%d 00:00:00"),
    end.strftime("%Y-%m-%d 23:59:59"),
    include_voided=False,
)
print(f"\nTotal non-voided shipments in the last 7 days: {len(shipments)}")

with_matching_wh = [s for s in shipments if s.get("warehouseId") in (warehouse_id, freight_id)]
with_any_wh = [s for s in shipments if s.get("warehouseId") is not None]
without_wh = [s for s in shipments if s.get("warehouseId") is None]

print(f"...with warehouseId matching our two known IDs: {len(with_matching_wh)}")
print(f"...with SOME warehouseId, but not matching either known ID: {len(with_any_wh) - len(with_matching_wh)}")
print(f"...with NO warehouseId field at all (null/missing): {len(without_wh)}")

print("\n=== Raw JSON of 3 sample shipments (to see the actual field) ===")
for s in shipments[:3]:
    print(json.dumps(s, indent=2))
    print("---")

if without_wh:
    print("\n=== Raw JSON of a shipment MISSING warehouseId (if any) ===")
    print(json.dumps(without_wh[0], indent=2))

# Second check: what statuses do real orders from the SAME real 2-week
# window Casey checked actually carry? "Orders In" only excludes
# "cancelled" -- if a meaningful number sit in some OTHER non-actionable
# status (e.g. awaiting_payment), that would explain orders being
# counted as "In" while never appearing anywhere else on the dashboard
# (not in the live queue, not Held, not Shipped) -- a real, previously
# invisible bucket, not necessarily a bug in the counting itself.
from collections import Counter

print("\n\n=== Order status breakdown, 2026-09-07 to 2026-09-18 ===")
all_orders = []
page = 1
while True:
    data = client._get("/orders", {
        "orderDateStart": "2026-09-07 00:00:00",
        "orderDateEnd": "2026-09-18 23:59:59",
        "page": page,
        "pageSize": 500,
    })
    batch = data.get("orders", [])
    all_orders.extend(batch)
    print(f"  page {page}/{data.get('pages', 1)} ({len(all_orders)} orders so far)")
    if page >= data.get("pages", 1):
        break
    page += 1

# Scope to Warehouse/Freight only, same as the real function.
scoped = [o for o in all_orders if (o.get("advancedOptions") or {}).get("warehouseId") in (warehouse_id, freight_id)]
print(f"\nTotal orders in window: {len(all_orders)}")
print(f"Warehouse/Freight-scoped: {len(scoped)}")

status_counts = Counter(o.get("orderStatus") for o in scoped)
print("\nStatus breakdown (Warehouse/Freight-scoped only):")
for status, count in status_counts.most_common():
    print(f"  {status}: {count}")

# Third check: of the orders that show orderStatus == "shipped", how
# many are externally fulfilled (shipped outside ShipStation entirely,
# meaning they'd likely never generate a normal /shipments record) vs
# how many actually have a matching shipment record in our /shipments
# results? If a meaningful chunk of "shipped" orders have NO matching
# shipment record, that's a permanent, structural blind spot in
# ship-date-based counting -- not something that resolves with a wider
# date window.
print("\n\n=== Checking 'shipped' orders for externallyFulfilled + matching shipment records ===")
shipped_orders = [o for o in scoped if o.get("orderStatus") == "shipped"]
externally_fulfilled = [o for o in shipped_orders if o.get("externallyFulfilled")]
print(f"'shipped' orders in this window: {len(shipped_orders)}")
print(f"...of which externallyFulfilled=true: {len(externally_fulfilled)}")

# Pull ALL shipments from the order window through today (wide enough
# to catch orders that shipped well after the original window closed,
# but still a bounded, sensible range).
all_shipments_ever = []
page = 1
today_str = datetime.now().strftime("%Y-%m-%d 23:59:59")
while True:
    data = client._get("/shipments", {
        "shipDateStart": "2026-09-07 00:00:00",
        "shipDateEnd": today_str,
        "page": page,
        "pageSize": 500,
        "includeShipmentItems": "false",
    })
    batch = data.get("shipments", [])
    all_shipments_ever.extend(batch)
    print(f"  shipments page {page}/{data.get('pages', 1)} ({len(all_shipments_ever)} so far)")
    if page >= data.get("pages", 1) or page >= 20:  # safety cap
        break
    page += 1
shipped_order_numbers_with_shipment = {s.get("orderNumber") for s in all_shipments_ever}

no_matching_shipment = [
    o for o in shipped_orders
    if o.get("orderNumber") not in shipped_order_numbers_with_shipment
]
print(f"...of which have NO matching record in a broad /shipments pull: {len(no_matching_shipment)}")
if no_matching_shipment:
    print("\nSample order marked 'shipped' with no matching shipment record:")
    print(json.dumps(no_matching_shipment[0], indent=2))
