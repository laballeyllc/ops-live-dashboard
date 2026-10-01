#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Pulls
real "Lot ID unprefixed" data from the Stock Quantity by Sublocation
report, so we can confirm the actual format before building the
same-lot fulfillment check column.

Usage:
    python debug_lot_data.py
"""
import json
from collections import defaultdict
from dotenv import load_dotenv
from ops_common import fetch_finale_report, STOCK_BY_SUBLOCATION_REPORT_URL, is_usable_sublocation

load_dotenv()

rows = fetch_finale_report(STOCK_BY_SUBLOCATION_REPORT_URL)
print(f"Total rows: {len(rows)}")

# 1. Sample of raw lot ID values, to confirm the real format
print("\n=== Sample 'Lot ID unprefixed' values (first 20 non-blank) ===")
seen = 0
for r in rows:
    lot = r.get("Lot ID unprefixed")
    if lot:
        qty = r.get("Units\nQoH")
        print(f"  {lot!r}  (Product ID: {r.get('Product ID')!r}, Sublocation: {r.get('Sublocation')!r}, Qty: {qty})")
        seen += 1
        if seen >= 20:
            break
if seen == 0:
    print("  No non-blank Lot ID unprefixed values found in any row!")

# 2. What fraction of USABLE-location rows actually have a lot ID?
usable_rows = [r for r in rows if is_usable_sublocation(r.get("Sublocation") or "")]
with_lot = [r for r in usable_rows if r.get("Lot ID unprefixed")]
print(f"\n=== Coverage ===")
print(f"Usable-location rows: {len(usable_rows)}")
print(f"...of which have a Lot ID unprefixed value: {len(with_lot)}")

# 3. Pick one real product with multiple lots (if any), to see a
# realistic same-lot-vs-split-across-lots example.
by_product = defaultdict(list)
for r in with_lot:
    pid = (r.get("Product ID") or "").strip()
    if pid:
        by_product[pid].append(r)

multi_lot_products = {pid: lots for pid, lots in by_product.items() if len(lots) > 1}
print(f"\n=== Products with stock in more than one lot ({len(multi_lot_products)} found) ===")
for pid, lots in list(multi_lot_products.items())[:5]:
    print(f"\n  Product: {pid}")
    for r in lots:
        qty = r.get("Units\nQoH")
        print(f"    Lot {r.get('Lot ID unprefixed')!r} @ {r.get('Sublocation')!r}: {qty} units")

# 4. A few real examples of usable-location rows with NO lot ID at all,
# so we can see what's actually different about them (a different
# product type? a different sublocation pattern? something else?).
print(f"\n=== Sample usable-location rows with NO Lot ID unprefixed ===")
no_lot = [r for r in usable_rows if not r.get("Lot ID unprefixed")]
for r in no_lot[:10]:
    qty = r.get("Units\nQoH")
    print(f"  Product ID: {r.get('Product ID')!r}, Sublocation: {r.get('Sublocation')!r}, Qty: {qty}, Description: {r.get('Description')!r}")
