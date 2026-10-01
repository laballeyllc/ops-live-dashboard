#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline.

Compares the SAME order's data from two different ShipStation endpoints:
  1. get_order_by_number() — a single-order lookup (already confirmed
     this includes advancedOptions.warehouseId for order 000406235)
  2. list_orders() — the BULK list endpoint our actual pipeline uses

If warehouseId is present in #1 but missing/different in #2, that
explains why the pipeline's classification keeps coming back blank
despite the single-order lookup showing the field populated — the two
endpoints may simply return different levels of detail per order.

Usage:
    python debug_order.py 000406235
"""
import sys
import json
from dotenv import load_dotenv
from shipstation_client import ShipStationClient

load_dotenv()

order_number = sys.argv[1] if len(sys.argv) > 1 else "000406235"

client = ShipStationClient()

print("=== Via get_order_by_number() (single-order lookup) ===")
single = client.get_order_by_number(order_number)
if single is None:
    print(f"No order found for order number {order_number}")
else:
    print(json.dumps(single.get("advancedOptions"), indent=2))
    print("userId:", single.get("userId"))

print()
print("=== Via list_orders() (bulk list endpoint — what the real pipeline uses) ===")
bulk_orders = client.list_orders(order_status="awaiting_shipment")
match = next((o for o in bulk_orders if str(o.get("orderNumber")) == str(order_number)), None)
if match is None:
    print(f"Order {order_number} not found in the awaiting_shipment bulk list "
          f"(checked {len(bulk_orders)} orders) — trying on_hold...")
    bulk_orders = client.list_orders(order_status="on_hold")
    match = next((o for o in bulk_orders if str(o.get("orderNumber")) == str(order_number)), None)

if match is None:
    print(f"Order {order_number} not found in either bulk list.")
else:
    print(json.dumps(match.get("advancedOptions"), indent=2))
    print("userId:", match.get("userId"))

print()
print("=== Checking get_warehouse_id() return values and types ===")
from ops_common import WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME
warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
print(f"get_warehouse_id('{WAREHOUSE_LOCATION_NAME}') = {warehouse_id!r} (type: {type(warehouse_id).__name__})")
print(f"get_warehouse_id('{FREIGHT_LOCATION_NAME}') = {freight_id!r} (type: {type(freight_id).__name__})")

order_data = match or single or {}
order_wh_id = (order_data.get("advancedOptions") or {}).get("warehouseId")
print(f"Order's advancedOptions.warehouseId = {order_wh_id!r} (type: {type(order_wh_id).__name__})")
print()
print("Does it match freight_id?", order_wh_id == freight_id)
print("Does it match warehouse_id?", order_wh_id == warehouse_id)
