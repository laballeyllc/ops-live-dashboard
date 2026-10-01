#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Pulls
the most recent real order(s) and prints every raw timestamp field
exactly as ShipStation's API returns them (no parsing, no timezone
conversion applied) — so we can compare directly against a known,
human-confirmed reference time from ShipStation's own UI.

This settles a serious open question: does the ShipStation v1 API
return timestamps in the account's configured timezone (Central, which
this whole project has assumed), or always in fixed US Pacific time
regardless of account settings (as several third-party integration
docs claim)? Everything in this project that reads order_datetime,
computes business hours, or drives the 48-hour SLA clock depends on
getting this right.

Usage:
    python debug_timezone_check.py
"""
import json
from dotenv import load_dotenv
from shipstation_client import ShipStationClient

load_dotenv()

client = ShipStationClient()

# Most recently modified orders -- the order Casey just placed should
# be at or near the top.
data = client._get("/orders", {"sortBy": "ModifyDate", "sortDir": "DESC", "page": 1, "pageSize": 5})
orders = data.get("orders", [])

print("=== 5 most recently modified orders — raw timestamp fields, no conversion applied ===\n")
for o in orders:
    print(f"Order {o.get('orderNumber')}:")
    print(f"  orderDate:  {o.get('orderDate')}")
    print(f"  createDate: {o.get('createDate')}")
    print(f"  modifyDate: {o.get('modifyDate')}")
    print()

print("Compare the orderDate/createDate/modifyDate above against the REAL, human-confirmed")
print("time from ShipStation's own UI (09/23/2026 17:12 Central) for whichever of these")
print("is the order placed ~11 minutes ago. If the raw field matches 17:12 exactly, the API")
print("is returning Central time (our assumption was correct). If it shows 15:12 (2 hours")
print("earlier), the API is returning Pacific time regardless of account settings, and we")
print("have a real, project-wide bug to fix.")
