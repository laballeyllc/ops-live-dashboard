#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Checks
whether the batch-job fix (shipDate vs modifyDate gap > 2 days) actually
explains the pre-8am / post-4pm spikes Casey is still seeing on the
hourly Orders Out chart, or whether this is a DIFFERENT, more
persistent pattern the existing fix wouldn't catch (e.g. a recurring
daily process with a modifyDate close to the real shipDate).

Usage:
    python debug_hourly_gap_check.py
"""
import json
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv
from shipstation_client import ShipStationClient
from ops_common import parse_shipstation_naive, CHICAGO_TZ

load_dotenv()

client = ShipStationClient()

# A real recent single day -- adjust the number of days back to check
# a different one.
TARGET_DATE = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
print(f"Checking orders shipped on {TARGET_DATE}\n")

modify_start = (datetime.strptime(TARGET_DATE, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d 00:00:00")
modify_end = (datetime.strptime(TARGET_DATE, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d 23:59:59")

data = client._get("/orders", {
    "orderStatus": "shipped",
    "modifyDateStart": modify_start,
    "modifyDateEnd": modify_end,
    "page": 1,
    "pageSize": 500,
})
orders = [o for o in data.get("orders", []) if (o.get("shipDate") or "")[:10] == TARGET_DATE]
print(f"Orders that shipped on {TARGET_DATE}: {len(orders)}\n")

pre_8am_or_post_4pm = []
for o in orders:
    modify_date = o.get("modifyDate")
    if not modify_date:
        continue
    instant = parse_shipstation_naive(modify_date)
    if not instant:
        continue
    central = instant.astimezone(CHICAGO_TZ)
    if central.hour < 8 or central.hour >= 16:
        gap_days = abs((datetime.strptime(o["modifyDate"][:10], "%Y-%m-%d") -
                         datetime.strptime(TARGET_DATE, "%Y-%m-%d")).days)
        pre_8am_or_post_4pm.append((o, central, gap_days))

print(f"=== {len(pre_8am_or_post_4pm)} orders with modifyDate outside 8am-4pm Central ===\n")
gap_counter = Counter()
for o, central, gap_days in pre_8am_or_post_4pm:
    gap_counter[gap_days] += 1

print("Gap between modifyDate's calendar date and real shipDate, across these orders:")
for gap, count in sorted(gap_counter.items()):
    caught_by_existing_fix = "EXCLUDED by current fix" if gap > 2 else "NOT caught by current fix"
    print(f"  {gap} day(s) apart: {count} orders -- {caught_by_existing_fix}")

print("\n=== Sample of 5 orders NOT caught by the current fix (gap <= 2 days) ===")
uncaught = [(o, c) for o, c, g in pre_8am_or_post_4pm if g <= 2]
for o, central in uncaught[:5]:
    print(f"Order {o.get('orderNumber')}: modifyDate {o.get('modifyDate')} -> Central {central.strftime('%H:%M:%S')}, "
          f"shipDate {o.get('shipDate')}, externallyFulfilled {o.get('externallyFulfilled')}, "
          f"orderStatus {o.get('orderStatus')}")

# New check: are the uncaught orders clustered into a small number of
# batch events (many orders sharing a near-identical modifyDate), or
# genuinely spread out (which would look more like real activity)?
print(f"\n=== Clustering check across all {len(uncaught)} uncaught orders ===")
from datetime import datetime as dt
timestamps = []
for o, central in uncaught:
    try:
        ts = dt.strptime(o["modifyDate"][:26], "%Y-%m-%dT%H:%M:%S.%f")
        timestamps.append((ts, o.get("orderNumber"), central))
    except ValueError:
        continue
timestamps.sort()

clusters = []
current_cluster = []
for ts, order_num, central in timestamps:
    if current_cluster and (ts - current_cluster[-1][0]).total_seconds() > 5:
        clusters.append(current_cluster)
        current_cluster = []
    current_cluster.append((ts, order_num, central))
if current_cluster:
    clusters.append(current_cluster)

print(f"{len(clusters)} distinct cluster(s) found (orders within 5 seconds of each other grouped together):")
for i, cluster in enumerate(clusters):
    first_central = cluster[0][2]
    print(f"  Cluster {i+1}: {len(cluster)} orders, all around {first_central.strftime('%H:%M:%S')} Central")
