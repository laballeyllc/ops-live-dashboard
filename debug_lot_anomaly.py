#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline.

1. Investigates the "0.007824" lot ID anomaly on EAS40B200-1GAL directly
   against the raw Finale report (not our own processed stock_levels
   data), to see what's actually in that row.
2. Independently checks, across ALL multi-lot products in the current
   real stock data, how often the largest single lot actually covers a
   representative small order quantity — computed fresh from Finale,
   not through any of our own dashboard logic, so a bug in that logic
   couldn't hide itself from this check.

Usage:
    python debug_lot_anomaly.py
"""
import json
from dotenv import load_dotenv
from ops_common import fetch_finale_report, STOCK_BY_SUBLOCATION_REPORT_URL, is_usable_sublocation

load_dotenv()

rows = fetch_finale_report(STOCK_BY_SUBLOCATION_REPORT_URL)
print(f"Total rows: {len(rows)}")

# 1. The specific anomaly: every raw row for EAS40B200-1GAL.
print("\n=== All raw rows for EAS40B200-1GAL ===")
target_rows = [r for r in rows if (r.get("Product ID") or "").strip() == "EAS40B200-1GAL"]
for r in target_rows:
    print(json.dumps(r, indent=2))
    print("---")

# 2. Independent lot aggregation + same-lot-pass check, built fresh from
# the raw report (not reusing fetch_usable_stock or any dashboard code),
# for every product with usable stock split across 2+ lots.
from collections import defaultdict

usable_lots_by_product = defaultdict(lambda: defaultdict(float))
for r in rows:
    sub = r.get("Sublocation")
    pid = (r.get("Product ID") or "").strip()
    if not sub or not pid or not is_usable_sublocation(sub):
        continue
    qoh = r.get("Units\nQoH")
    if not isinstance(qoh, (int, float)):
        continue
    lot_id = (r.get("Lot ID unprefixed") or "").strip()
    if lot_id:
        usable_lots_by_product[pid][lot_id] += qoh

multi_lot_products = {pid: lots for pid, lots in usable_lots_by_product.items() if len(lots) > 1}
print(f"\n\n=== Independent check: {len(multi_lot_products)} products with usable stock in 2+ lots ===")

# For a representative small order quantity (using 1, 2, and 4 units as
# stand-ins for typical real order sizes), how often does the SINGLE
# LARGEST lot actually cover it?
for test_qty in (1, 2, 4):
    covered = sum(1 for lots in multi_lot_products.values() if max(lots.values()) >= test_qty)
    pct = covered / len(multi_lot_products) * 100 if multi_lot_products else 0
    print(f"  Needing {test_qty}: {covered}/{len(multi_lot_products)} products ({pct:.0f}%) have a single lot that alone covers it")

# Flag any lot ID that doesn't match either known real format, across
# the WHOLE dataset -- not just the one anomaly already found.
import re
PO_PATTERN = re.compile(r"^\d+/")
INHOUSE_PATTERN = re.compile(r"^U-\d{6}-")
print("\n=== Any OTHER lot IDs that don't match either known format ===")
unrecognized = set()
for pid, lots in usable_lots_by_product.items():
    for lot_id in lots:
        if not PO_PATTERN.match(lot_id) and not INHOUSE_PATTERN.match(lot_id):
            unrecognized.add((pid, lot_id))
if unrecognized:
    for pid, lot_id in sorted(unrecognized):
        print(f"  Product {pid!r}: unrecognized lot ID {lot_id!r}")
else:
    print("  None found -- the EAS40B200-1GAL case looks like an isolated anomaly.")
