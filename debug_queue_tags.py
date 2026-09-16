#!/usr/bin/env python3
"""
Diagnostic only — not part of daily automation.

Lists the actual order numbers counted for each queue, so you can
cross-check specific orders against the Warehouse / Freight tabs in the
ShipStation UI directly, rather than just comparing totals (which drift
constantly since new orders arrive by the second).

Usage:
    python debug_queue_tags.py
"""
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from pull_stats import WAREHOUSE_TAG_NAME, FREIGHT_TAG_NAME

load_dotenv()
client = ShipStationClient()

warehouse_tag_id = client.get_tag_id(WAREHOUSE_TAG_NAME)
freight_tag_id = client.get_tag_id(FREIGHT_TAG_NAME)
print(f"Warehouse tag id: {warehouse_tag_id} ({WAREHOUSE_TAG_NAME})")
print(f"Freight tag id: {freight_tag_id} ({FREIGHT_TAG_NAME})")
print()

orders = client.list_orders(order_status="awaiting_shipment")
print(f"Total awaiting_shipment orders fetched: {len(orders)}")
print()

warehouse_orders = [o for o in orders if warehouse_tag_id in (o.get("tagIds") or [])]
freight_orders = [o for o in orders if freight_tag_id in (o.get("tagIds") or [])]
both = [o for o in orders if warehouse_tag_id in (o.get("tagIds") or []) and freight_tag_id in (o.get("tagIds") or [])]
neither = [o for o in orders if warehouse_tag_id not in (o.get("tagIds") or []) and freight_tag_id not in (o.get("tagIds") or [])]

print(f"Warehouse-tagged: {len(warehouse_orders)}")
print(f"Freight-tagged: {len(freight_orders)}")
print(f"Tagged BOTH (would double-count if summed): {len(both)}")
print(f"Tagged NEITHER (not in either queue): {len(neither)}")
print()

print("First 10 Warehouse order numbers (check these against the Warehouse tab):")
for o in warehouse_orders[:10]:
    print(f"  {o.get('orderNumber')}  (tagIds={o.get('tagIds')})")

print()
print("First 10 Freight order numbers (check these against the Freight tab):")
for o in freight_orders[:10]:
    print(f"  {o.get('orderNumber')}  (tagIds={o.get('tagIds')})")

if neither:
    print()
    print("First 5 orders tagged with NEITHER Warehouse nor Freight:")
    for o in neither[:5]:
        print(f"  {o.get('orderNumber')}  (tagIds={o.get('tagIds')})")