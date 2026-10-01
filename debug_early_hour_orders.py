#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline.
Investigates orders whose modifyDate falls at an implausible hour (the
"shipping out before clock-in" symptom Casey noticed on the hourly
chart) — before touching any timezone code, since the underlying
Pacific-time finding was already verified with exact, real evidence
(a real order's raw timestamp matched ShipStation's own UI exactly,
to the second, at a precise 2-hour offset).

The leading alternate theory: modifyDate is only ever an APPROXIMATION
of ship time (ShipStation has no real ship-time field at all — shipDate
is date-only, confirmed earlier). Automated processes (batch syncs,
overnight imports, marketplace status pushes) can bump modifyDate with
no human involved, which would produce exactly this symptom without
the timezone math being wrong.

Usage:
    python debug_early_hour_orders.py
"""
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from ops_common import parse_shipstation_naive, CHICAGO_TZ

load_dotenv()

client = ShipStationClient()

# Pull recent shipped orders and check which ones have a modifyDate
# that converts to an implausible Central hour (midnight-6am, when no
# one is working).
end = datetime.now()
start = end - timedelta(days=3)
data = client._get("/orders", {
    "orderStatus": "shipped",
    "modifyDateStart": start.strftime("%Y-%m-%d 00:00:00"),
    "modifyDateEnd": end.strftime("%Y-%m-%d 23:59:59"),
    "page": 1,
    "pageSize": 500,
})
orders = data.get("orders", [])
print(f"Checked {len(orders)} recently-modified shipped orders\n")

suspicious = []
for o in orders:
    modify_date = o.get("modifyDate")
    if not modify_date:
        continue
    instant = parse_shipstation_naive(modify_date)
    if not instant:
        continue
    central = instant.astimezone(CHICAGO_TZ)
    if 0 <= central.hour < 6:
        suspicious.append((o, central))

print(f"=== {len(suspicious)} orders with modifyDate landing between midnight-6am Central ===\n")
for o, central in suspicious[:10]:
    print(f"Order {o.get('orderNumber')}:")
    print(f"  raw modifyDate: {o.get('modifyDate')}")
    print(f"  -> Central time: {central.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  orderDate: {o.get('orderDate')}")
    print(f"  shipDate: {o.get('shipDate')}")
    print(f"  externallyFulfilled: {o.get('externallyFulfilled')}")
    print(f"  createDate: {o.get('createDate')}")
    print()
