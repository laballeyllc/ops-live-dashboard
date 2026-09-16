#!/usr/bin/env python3
"""
Diagnostic only. Prints the raw structure of a few of today's shipment
records so we can confirm which fields (orderId, orderNumber, warehouseId)
are actually populated for this account.

Usage:
    python debug_shipments.py
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from pull_stats import ss_datetime

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
print()

if not shipments:
    print("No shipments recorded yet today — try again later in the day.")
else:
    print("First 3 raw shipment records:")
    for s in shipments[:3]:
        print(json.dumps(s, indent=2, default=str))
        print("---")

    print()
    print("Field presence check across all of today's shipments:")
    has_order_id = sum(1 for s in shipments if s.get("orderId"))
    has_order_number = sum(1 for s in shipments if s.get("orderNumber"))
    has_warehouse_id = sum(1 for s in shipments if s.get("warehouseId") is not None)
    print(f"  orderId present:     {has_order_id} / {len(shipments)}")
    print(f"  orderNumber present: {has_order_number} / {len(shipments)}")
    print(f"  warehouseId present: {has_warehouse_id} / {len(shipments)}")
