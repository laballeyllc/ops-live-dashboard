#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Checks
whether known dropship vendor orders (Biologix, United Scientific,
Transene, Post Apple Scientific, GT) are being correctly excluded from
our Warehouse/Freight scoping, or whether some of them carry a
warehouseId matching ours despite being externally fulfilled by a
vendor — which would mean the vendor's own shipping schedule (nothing
to do with our shift hours) could be feeding into the Orders Out hourly
numbers.

Usage:
    python debug_dropship_scoping.py
"""
import json
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from ops_common import WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME

load_dotenv()

client = ShipStationClient()
warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
valid_ids = {warehouse_id, freight_id}
print(f"Our known warehouse IDs: Warehouse={warehouse_id}, Freight={freight_id}\n")

DROPSHIP_VENDOR_KEYWORDS = ["biologix", "united scientific", "transene", "post apple", "gt"]

end = datetime.now()
start = end - timedelta(days=14)
all_orders = []
page = 1
while True:
    data = client._get("/orders", {
        "orderStatus": "shipped",
        "modifyDateStart": start.strftime("%Y-%m-%d 00:00:00"),
        "modifyDateEnd": end.strftime("%Y-%m-%d 23:59:59"),
        "page": page,
        "pageSize": 500,
    })
    batch = data.get("orders", [])
    all_orders.extend(batch)
    if page >= data.get("pages", 1):
        break
    page += 1

print(f"Total shipped orders checked (last 14 days): {len(all_orders)}\n")

# Check every order's tags and notes for dropship vendor mentions, and
# whether externallyFulfilled + warehouseId together look suspicious.
externally_fulfilled_with_our_warehouse = []
for o in all_orders:
    wh_id = (o.get("advancedOptions") or {}).get("warehouseId")
    is_ext = o.get("externallyFulfilled")
    if is_ext and wh_id in valid_ids:
        externally_fulfilled_with_our_warehouse.append(o)

print(f"=== Externally-fulfilled orders that STILL carry OUR warehouseId: "
      f"{len(externally_fulfilled_with_our_warehouse)} ===\n")

by_fulfilled_name = Counter(
    (o.get("externallyFulfilledByName") or o.get("externallyFulfilledBy") or "(unnamed)")
    for o in externally_fulfilled_with_our_warehouse
)
print("Broken down by who externally fulfilled them:")
for name, count in by_fulfilled_name.most_common():
    print(f"  {name}: {count}")

print(f"\n=== Sample of 5 externally-fulfilled orders with our warehouseId ===")
for o in externally_fulfilled_with_our_warehouse[:5]:
    print(f"Order {o.get('orderNumber')}: warehouseId={o.get('advancedOptions', {}).get('warehouseId')}, "
          f"externallyFulfilledBy={o.get('externallyFulfilledBy')}, "
          f"externallyFulfilledByName={o.get('externallyFulfilledByName')}, "
          f"internalNotes={(o.get('internalNotes') or '')[:80]!r}")

# Also directly check store/tag names for known dropship vendor keywords.
print(f"\n=== Checking ALL {len(all_orders)} orders for dropship vendor name mentions (tags/notes) ===")
vendor_matches = []
for o in all_orders:
    notes = f"{o.get('internalNotes') or ''} {o.get('customerNotes') or ''}".lower()
    for kw in DROPSHIP_VENDOR_KEYWORDS:
        if kw in notes:
            vendor_matches.append((o, kw))
            break

print(f"Orders mentioning a known dropship vendor in their notes: {len(vendor_matches)}")
for o, kw in vendor_matches[:10]:
    wh_id = (o.get("advancedOptions") or {}).get("warehouseId")
    in_scope = wh_id in valid_ids
    print(f"  Order {o.get('orderNumber')}: matched {kw!r}, warehouseId={wh_id}, "
          f"currently counted as ours: {in_scope}")
