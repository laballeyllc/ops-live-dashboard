#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Checks
the real column structure of the Product Bill of Materials report
before building classification logic on top of assumptions.

Usage:
    python debug_bom_structure.py
"""
import json
from dotenv import load_dotenv
from ops_common import fetch_finale_report, PRODUCT_BOM_REPORT_URL

load_dotenv()

rows = fetch_finale_report(PRODUCT_BOM_REPORT_URL)
print(f"Total BOM rows: {len(rows)}\n")

print("=== First 5 raw rows ===")
for r in rows[:5]:
    print(json.dumps(r, indent=2))
    print("---")

# Sanity check: does any Product ID appear as BOTH a parent and a
# component somewhere else? (e.g. a sub-assembly that's itself built
# from other components, but also used as an ingredient elsewhere)
if rows:
    sample_keys = list(rows[0].keys())
    print(f"\nColumn names found: {sample_keys}")
