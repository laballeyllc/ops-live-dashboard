#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Measures
what SHARE of all shipped orders (not just the ones landing outside
8am-4pm) show the batch-touch signature (multiple orders sharing a
near-identical modifyDate) -- this is the real question: is the
automated-process contamination a small, ignorable minority of orders,
or does it run continuously enough that most modifyDate values can't be
trusted, even during business hours where the contamination is
invisible (mixed in with real activity)?

Usage:
    python debug_contamination_rate.py
"""
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from shipstation_client import ShipStationClient

load_dotenv()

client = ShipStationClient()

TARGET_DATE = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
print(f"Checking ALL orders shipped on {TARGET_DATE} (not just outside-hours ones)\n")

modify_start = (datetime.strptime(TARGET_DATE, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
modify_end = (datetime.strptime(TARGET_DATE, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")

data = client._get("/orders", {
    "orderStatus": "shipped",
    "modifyDateStart": modify_start,
    "modifyDateEnd": modify_end,
    "page": 1,
    "pageSize": 500,
})
orders = [o for o in data.get("orders", []) if (o.get("shipDate") or "")[:10] == TARGET_DATE]
print(f"Total orders shipped on {TARGET_DATE}: {len(orders)}\n")

# Cluster ALL modifyDate timestamps (regardless of hour) by proximity --
# orders within 5 seconds of another order are treated as one batch
# touch, the same signature confirmed earlier.
from datetime import datetime as dt
timestamps = []
for o in orders:
    md = o.get("modifyDate")
    if not md:
        continue
    try:
        ts = dt.strptime(md[:26], "%Y-%m-%dT%H:%M:%S.%f")
        timestamps.append((ts, o.get("orderNumber")))
    except ValueError:
        continue
timestamps.sort()

clusters = []
current_cluster = []
for ts, order_num in timestamps:
    if current_cluster and (ts - current_cluster[-1][0]).total_seconds() > 5:
        clusters.append(current_cluster)
        current_cluster = []
    current_cluster.append((ts, order_num))
if current_cluster:
    clusters.append(current_cluster)

batch_clusters = [c for c in clusters if len(c) > 1]
isolated = [c for c in clusters if len(c) == 1]

orders_in_batch_clusters = sum(len(c) for c in batch_clusters)
orders_isolated = sum(len(c) for c in isolated)

print(f"=== Clustering across the FULL day (all hours, not just outside 8am-4pm) ===")
print(f"Total orders with a modifyDate: {len(timestamps)}")
print(f"Orders sharing a near-identical modifyDate with at least one other order "
      f"(batch-touch signature): {orders_in_batch_clusters} ({orders_in_batch_clusters/len(timestamps)*100:.1f}%)")
print(f"Orders with a unique, isolated modifyDate (no other order within 5 seconds): "
      f"{orders_isolated} ({orders_isolated/len(timestamps)*100:.1f}%)")
print(f"\nNumber of distinct batch-touch events: {len(batch_clusters)}")
print(f"Average orders per batch event: {orders_in_batch_clusters/len(batch_clusters):.1f}" if batch_clusters else "N/A")

# Break down by hour to see whether contamination is spread evenly
# across the day or concentrated at particular times.
from collections import Counter
from ops_common import parse_shipstation_naive, CHICAGO_TZ

hour_totals = Counter()
hour_batch = Counter()
order_to_batch = set()
for c in batch_clusters:
    for ts, order_num in c:
        order_to_batch.add(order_num)

for o in orders:
    md = o.get("modifyDate")
    if not md:
        continue
    instant = parse_shipstation_naive(md)
    if not instant:
        continue
    central_hour = instant.astimezone(CHICAGO_TZ).hour
    hour_totals[central_hour] += 1
    if o.get("orderNumber") in order_to_batch:
        hour_batch[central_hour] += 1

print(f"\n=== Batch-touch share by hour (Central) ===")
for h in sorted(hour_totals):
    total = hour_totals[h]
    batch = hour_batch[h]
    pct = batch / total * 100 if total else 0
    marker = " <-- inside 8am-4pm shift" if 8 <= h < 16 else ""
    print(f"  {h:02d}:00 - {batch}/{total} orders show batch signature ({pct:.0f}%){marker}")
