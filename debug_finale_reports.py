#!/usr/bin/env python3
"""
TEMPORARY diagnostic script — not part of the regular pipeline. Fetches
real sample data from the three new Finale reports so we can see actual
column names and a few real rows before writing any production code.

Usage:
    python debug_finale_reports.py
"""
import json
from dotenv import load_dotenv
from ops_common import fetch_finale_report

load_dotenv()

REPORTS = {
    "Stock quantity by location, in units w/ detail": (
        "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
        "1789740583812/Report.json?format=jsonObject&data=stock"
        "&attrName=%23%23stock027"
        "&rowDimensions=~lZrNA0XAzP7AwMDAAcDAms0B_sDLQGlmZmZmZmbAwMDAwMDAms0B1MDLQHPYAAAAAADAwMDAwMDAms0CC6lTdGRcblBrbmfLQGlmZmZmZmbAwMDAAcDAmjPAy0B-9MzMzMzNwMDAwMDAwA"
        "&metrics=~lJrNBLiqVW5pdHNcblFvSMtAaWZmZmZmZsDAwMDAwMCazQS7rVVuaXRzXG5QYWNrZWTLQGlmZmZmZmbAwMDAwMDAms0Ev65Vbml0c1xuVHJhbnNpdMtAaWZmZmZmZsDAwMDAwMCazQTAqlVuaXRzXG5XSVDLQGlmZmZmZmbAwMDAwMDA"
        "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fT05fSEFORCIsIlNUT0NLX0lURU1fSU5fVFJBTlNJVCIsIlNUT0NLX0lURU1fV0lQIiwiU1RPQ0tfSVRFTV9QQUNLRUQiXSxudWxsXSxbInByb2R1Y3RTdGF0dXMiLG51bGwsbnVsbF0sWyJwcm9kdWN0UHJvZHVjdFVybCIsbnVsbCxudWxsXSxbInByb2R1Y3RDYXRlZ29yeSIsbnVsbCxudWxsXSxbInByb2R1Y3RNYW51ZmFjdHVyZXIiLG51bGwsbnVsbF0sWyJzdG9ja0xvY2F0aW9uIixudWxsLG51bGxdLFsic3RvY2tFZmZlY3RpdmVEYXRlIixudWxsLG51bGxdXQ%3D%3D"
        "&reportTitle=Stock%20quantity%20by%20location%2C%20in%20units%20w%2F%20detail"
    ),
    "Backordered sales by order (SkD)": (
        "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
        "1789740640527/Report.json?format=jsonObject&data=stock"
        "&attrName=%23%23sale013"
        "&rowDimensions=~mJrNA2HAy0BpZmZmZmZmwMDAwMDAwJrNA2LAy0BpZmZmZmZmwMDAwMDAwJrNA2XAzP7AwMDAAcDAms0B_sDLQGlmZmZmZmbAwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAyOMDNAX3AwMDAwMDAms0BzcDM_sDAwMDAwMCauXN0b2NrT3JkZXJTaGlwcGluZ1NlcnZpY2XAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAzNcDM_sDAwMDAwMA"
        "&metrics=~m5rNBL6zVW5pdHMgcnN2ZFxub24gaGFuZMtAaWZmZmZmZsDAwMDAwMCazQS9tlVuaXRzIHJzdmRcbmJhY2sgb3JkZXLLQGlmZmZmZmbAwMDAwMDAms0Eu61Vbml0c1xucGFja2Vky0BpZmZmZmZmwMDAwMDAwJrZPXByb2R1Y3RSZW9yZGVyTGV2ZWxNYXhMYWJhbGxleWxsY2FwaWZhY2lsaXR5MTAwMzA4Q29uc29saWRhdGXAzP7AwMDAwMDAmr9wcm9kdWN0VXNlclVzZXIxMDAzMkNvbnNvbGlkYXRlsUxvdCBJRCB0byBQcm9kdWNlzP7AwMDAwMDAmr9wcm9kdWN0VXNlclVzZXIxMDAzMkNvbnNvbGlkYXRlwMz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzJDb25zb2xpZGF0ZatTdWJsb2NhdGlvbsz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMjhDb25zb2xpZGF0ZatEZXNjcmlwdGlvbsz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzJDb25zb2xpZGF0ZaVCbGFua8z-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzNDb25zb2xpZGF0ZcDM_sDAwMDAwMCav3Byb2R1Y3RVc2VyVXNlcjEwMDMyQ29uc29saWRhdGWmVmVyaWZ5zP7AwMDAwMDA"
        "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fUlNWRCIsIlNUT0NLX0lURU1fUEFDS0VEIl0sbnVsbF0sWyJwcm9kdWN0UHJvZHVjdFVybCIsbnVsbCxudWxsXSxbInByb2R1Y3RDYXRlZ29yeSIsbnVsbCxudWxsXSxbInByb2R1Y3RNYW51ZmFjdHVyZXIiLG51bGwsbnVsbF0sWyJwcm9kdWN0U3VwcGxpZXIiLG51bGwsbnVsbF0sWyJzdG9ja09yZGVyT3JkZXJVcmwiLG51bGwsbnVsbF0sWyJzdG9ja09yZGVyT3JpZ2luIixudWxsLG51bGxdLFsic3RvY2tPcmRlck9yZGVyRGF0ZSIsIltudWxsLG51bGxdIixudWxsXSxbInN0b2NrT3JkZXJDdXN0b21lciIsbnVsbCxudWxsXSxbInN0b2NrTG9jYXRpb24iLG51bGwsbnVsbF1d"
        "&reportTitle=Backordered%20sales%20by%20order%20(SkD)"
    ),
    "Stock Quantity by Sublocation In Units": (
        "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
        "1789740705597/Report.json?format=jsonObject&data=stock"
        "&attrName=%23%23stock030"
        "&rowDimensions=~lZrNA0erU3VibG9jYXRpb27M_sDAwMDAwMCazQH-wMtAaWZmZmZmZsDAwMDAwMCazQHUwMtAiWZmZmZmZsDAwMDAwMCazQILqVN0ZFxuUGtuZ8tAaWZmZmZmZsDAwMDAwMCatHN0b2NrTG90SWRVbnByZWZpeGVkwMz-wMDAwMDAwA"
        "&metrics=~lZrNBLiqVW5pdHNcblFvSMtAaWZmZmZmZsDAwMDAwMCazQS7rVVuaXRzXG5QYWNrZWTLQGlmZmZmZmbAwMDAwMDAms0Ev65Vbml0c1xuVHJhbnNpdMtAaWZmZmZmZsDAwMDAwMCazQTAqlVuaXRzXG5XSVDLQGlmZmZmZmbAwMDAwMDAmr9zdG9ja0xvdElkVW5wcmVmaXhlZENvbnNvbGlkYXRlwMz-wMDAwMDAwA"
        "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fT05fSEFORCIsIlNUT0NLX0lURU1fSU5fVFJBTlNJVCIsIlNUT0NLX0lURU1fV0lQIiwiU1RPQ0tfSVRFTV9QQUNLRUQiXSxudWxsXSxbInByb2R1Y3RTdGF0dXMiLFsiUFJPRFVDVF9BQ1RJVkUiXSxudWxsXSxbInByb2R1Y3RQcm9kdWN0VXJsIixudWxsLG51bGxdLFsicHJvZHVjdENhdGVnb3J5IixudWxsLG51bGxdLFsicHJvZHVjdE1hbnVmYWN0dXJlciIsbnVsbCxudWxsXSxbInN0b2NrTG9jYXRpb24iLG51bGwsbnVsbF0sWyJzdG9ja01hZ2F6aW5lIixudWxsLG51bGxdLFsic3RvY2tFZmZlY3RpdmVEYXRlIixudWxsLG51bGxdXQ%3D%3D"
        "&reportTitle=Stock%20Quantity%20by%20Sublocation%20In%20Units"
    ),
}

for name, url in REPORTS.items():
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    try:
        rows = fetch_finale_report(url)
        print(f"Total rows: {len(rows)}")
        if rows:
            print(f"Columns: {list(rows[0].keys())}")
            print("\nFirst 3 rows:")
            for r in rows[:3]:
                print(json.dumps(r, indent=2))
    except Exception as e:
        print(f"ERROR fetching this report: {e}")

# Find a few rows where reorder point max is actually populated (non-null,
# non-blank), so we can see a real example rather than just null samples.
print(f"\n{'='*70}\nSearching for populated 'reorder point max' examples\n{'='*70}")
backorder_rows = fetch_finale_report(REPORTS["Backordered sales by order (SkD)"])
found = 0
for r in backorder_rows:
    val = r.get("DrippingSprings\nreorder point max")
    if val not in (None, "", " "):
        print(json.dumps(r, indent=2))
        found += 1
        if found >= 3:
            break
print(f"\nFound {found} populated examples out of {len(backorder_rows)} total rows")

# Full distinct list of sublocation names from the stock report, so we
# can verify the "usable location" allowlist pattern against everything
# real rather than just a few samples.
print(f"\n{'='*70}\nAll distinct sublocation names in 'Stock Quantity by Sublocation In Units'\n{'='*70}")
stock_rows = fetch_finale_report(REPORTS["Stock Quantity by Sublocation In Units"])
distinct_sublocations = sorted(set(
    r.get("Sublocation") for r in stock_rows if r.get("Sublocation")
))
print(f"{len(distinct_sublocations)} distinct sublocation names:\n")
for s in distinct_sublocations:
    print(f"  {s!r}")

