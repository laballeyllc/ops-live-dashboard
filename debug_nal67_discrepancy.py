#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline.
Investigates a real discrepancy Casey found: NAL67-2.5L shows 0 usable
stock and no reorder_point_max in our system, despite Finale's own UI
showing ~318 units on hand and a configured reorder point of 40.5/100.

Checks two separate things:
1. Does NAL67-2.5L actually appear in the raw Stock by Sublocation
   report, and if so, why would our usable_stock calculation zero it
   out? (product ID matching, sublocation pattern matching, etc.)
2. Does NAL67-2.5L appear in the Backordered Demand report at all? If
   not, that confirms reorder_point_max is only ever captured for
   products with CURRENT backorder demand — a structural gap that could
   affect the whole catalog, not just this one product.

Usage:
    python debug_nal67_discrepancy.py
"""
import json
from dotenv import load_dotenv
from ops_common import (
    fetch_finale_report, STOCK_BY_SUBLOCATION_REPORT_URL, BACKORDER_DEMAND_REPORT_URL,
    is_usable_sublocation, fetch_usable_stock, fetch_backorder_demand,
)

load_dotenv()

TARGET = "NAL67-2.5L"

# 1. Raw stock-by-sublocation rows for this exact product.
print(f"=== Raw Stock by Sublocation rows for {TARGET!r} ===")
stock_rows = fetch_finale_report(STOCK_BY_SUBLOCATION_REPORT_URL)
target_stock_rows = [r for r in stock_rows if (r.get("Product ID") or "").strip() == TARGET]
print(f"Found {len(target_stock_rows)} raw rows")
for r in target_stock_rows:
    sub = r.get("Sublocation")
    usable = is_usable_sublocation(sub) if sub else None
    print(f"  Sublocation={sub!r}, usable={usable}, Lot={r.get('Lot ID unprefixed')!r}, "
          f"QoH={r.get('Units' + chr(10) + 'QoH')}")

# Cross-check: what does fetch_usable_stock() actually compute for this product?
print(f"\n=== What fetch_usable_stock() computes for {TARGET!r} ===")
usable_stock_map = fetch_usable_stock()
entry = usable_stock_map.get(TARGET)
print(json.dumps(entry, indent=2) if entry else "NOT FOUND in usable_stock_map at all")

# 2. Does this product appear anywhere in the Backordered Demand report?
print(f"\n=== Checking Backordered Demand report for {TARGET!r} ===")
demand_rows = fetch_finale_report(BACKORDER_DEMAND_REPORT_URL)
target_demand_rows = [r for r in demand_rows if (r.get("Product ID") or "").strip() == TARGET]
print(f"Found {len(target_demand_rows)} raw rows mentioning this product in the demand report")
if target_demand_rows:
    for r in target_demand_rows:
        print(json.dumps(r, indent=2))
else:
    print("  Confirms: this product has NO current backorder demand, so it never appears")
    print("  in this report at all -- meaning we never capture its reorder_point_max,")
    print("  regardless of what's actually configured in Finale's product page.")

demand_map = fetch_backorder_demand()
print(f"\nDoes {TARGET!r} have an entry in fetch_backorder_demand()'s output?",
      TARGET in demand_map)

# 3. Broader check: of ALL products with usable stock, how many have NO
# reorder_point_max captured at all -- i.e. how widespread is this gap?
print(f"\n=== How widespread is this gap across the catalog? ===")
products_with_stock = set(usable_stock_map.keys())
products_with_reorder_data = set(demand_map.keys())
missing_reorder_data = products_with_stock - products_with_reorder_data
print(f"Products with usable stock: {len(products_with_stock)}")
print(f"Products with ANY reorder_point_max data captured: {len(products_with_reorder_data)}")
print(f"Products with stock but NO reorder_point_max captured at all: {len(missing_reorder_data)} "
      f"({len(missing_reorder_data) / max(1, len(products_with_stock)) * 100:.0f}% of the catalog)")
