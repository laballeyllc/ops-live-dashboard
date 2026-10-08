"""
Shared logic between pull_ops_data.py (the full historical pipeline) and
pull_live_queue.py (the current-queue-only, frequent-refresh script).
Pulled out here so both scripts use the exact same, already-verified
logic instead of two copies that could quietly drift apart.

Everything tag/queue related in this file was confirmed against real,
tab-checked ShipStation orders (000405560 in the Warehouse tab -> tag
"Austin Warehouse"; 000405437 in the Freight tab -> tag "ATX Freight") —
see the project history for how we got here. Don't change
CORE_QUEUE_TAGS without re-confirming against a real order the same way.
"""
import os
import re
import time
import json
import base64
import urllib.parse
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

FINALE_API_KEY = os.environ["FINALE_API_KEY"]
FINALE_API_SECRET = os.environ["FINALE_API_SECRET"]

# Reporting API URL for the Orders report ("Ops Live Dashboard"), via the
# direct pivotTable form (not pivotTableStream) per Finale's docs — this
# runs the report fresh, synchronously, on every call. If this report's
# columns are ever edited in the Finale UI, this URL may need
# re-capturing (Reports > Actions > Other exports > Export to JSON, then
# swap pivotTableStream -> pivotTable in the resulting URL).
ORDERS_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1789591213959/Report.json?format=jsonObject&data=order"
    "&attrName=%23%23user088"
    "&rowDimensions=~n5rM1sDM_sDAwMDAwMCazNXAzP7AwMDAwMDAms0B_sDM_sDAwMDAwMCazQHUwMz-wMDAwMDAwJqzb3JkZXJFbGlnaWJsZVRvU2hpcMDM_sDAwMDAwMCazQEFwMz-wMDAwMDAwJq2b3JkZXJTaGlwRnJvbUZvcm1hdHRlZMDM_sDAwMDAwMCazQHNwMz-wMDAwMDAwJq0cHJvZHVjdFVzZXJVc2VyMTAwMzPAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDA0MMDM_sDAwMDAwMCa2T5wcm9kdWN0U3RvcmVRdWFudGl0eVNvdXJjZUVudW1MYWJhbGxleWxsY2FwaXByb2R1Y3RzdG9yZTEwMDAwNMDM_sDAwMDAwMCazNvAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAzOcDM_sDAwMDAwMCatHByb2R1Y3RVc2VyVXNlcjEwMDAzwMz-wMDAwMDAwJq0cHJvZHVjdFVzZXJVc2VyMTAwMzXAzP7AwMDAwMDA"
    "&reportTitle=Ops%20Live%20Dashboard"
)
# Note: rowDimensions blob re-captured on 2026-09-16 to include the two
# new columns added that day: Product: Downpack Product and Product:
# HAZMAT (both confirmed added correctly, alongside the pre-existing
# Magento field, via direct data verification before saving).

# CONFIRMED (2026-09-16) full history of what's been tried and why each
# was wrong or incomplete:
#   1. Tags — a real order (000406235) was assigned to Freight purely via
#      a SKU-pattern automation rule that never touches tags at all, so
#      tag-based classification silently misclassified it as Warehouse.
#   2. Assigned user (order['userId']) — seemed right in principle (the
#      account owner confirmed every Freight order is assigned to user
#      "Jerry", every Warehouse order to user "Warehouse"), but
#      ShipStation's own GET /users endpoint returned only 13 users, and
#      neither the real "Jerry" nor "Warehouse" account's userId appeared
#      among them AT ALL — confirmed by pulling one order's raw JSON
#      directly (000406235's real userId, ca55e6e7-..., matched none of
#      the 13). Whatever that endpoint returns, it isn't the full roster
#      that order.userId actually references.
#   3. Ship From Location (advancedOptions.warehouseId) — what we're
#      using now. The same automation rule that assigns the user ALSO
#      sets this field in the same action ("Set Ship From Location:
#      Dripping Springs Freight"), so it carries the identical routing
#      signal, but resolves through list_warehouses()/get_warehouse_id(),
#      a completely different, already-proven endpoint from very early
#      in this project — sidestepping whatever gap exists in /users.
#      WAREHOUSE_LOCATION_NAME/FREIGHT_LOCATION_NAME below match the
#      exact location names seen in the automation rule screenshots.
CORE_QUEUE_TAGS = {"Austin Warehouse": "Warehouse", "ATX Freight": "Freight"}
# --- Stock/inventory reports (for usable-stock and reorder-point data,
# powering the "Attack the Queue" chemical prioritization) ---

STOCK_BY_SUBLOCATION_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1789740705597/Report.json?format=jsonObject&data=stock"
    "&attrName=%23%23stock030"
    "&rowDimensions=~lZrNA0erU3VibG9jYXRpb27M_sDAwMDAwMCazQH-wMtAaWZmZmZmZsDAwMDAwMCazQHUwMtAiWZmZmZmZsDAwMDAwMCazQILqVN0ZFxuUGtuZ8tAaWZmZmZmZsDAwMDAwMCatHN0b2NrTG90SWRVbnByZWZpeGVkwMz-wMDAwMDAwA"
    "&metrics=~lZrNBLiqVW5pdHNcblFvSMtAaWZmZmZmZsDAwMDAwMCazQS7rVVuaXRzXG5QYWNrZWTLQGlmZmZmZmbAwMDAwMDAms0Ev65Vbml0c1xuVHJhbnNpdMtAaWZmZmZmZsDAwMDAwMCazQTAqlVuaXRzXG5XSVDLQGlmZmZmZmbAwMDAwMDAmr9zdG9ja0xvdElkVW5wcmVmaXhlZENvbnNvbGlkYXRlwMz-wMDAwMDAwA"
    "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fT05fSEFORCIsIlNUT0NLX0lURU1fSU5fVFJBTlNJVCIsIlNUT0NLX0lURU1fV0lQIiwiU1RPQ0tfSVRFTV9QQUNLRUQiXSxudWxsXSxbInByb2R1Y3RTdGF0dXMiLFsiUFJPRFVDVF9BQ1RJVkUiXSxudWxsXSxbInByb2R1Y3RQcm9kdWN0VXJsIixudWxsLG51bGxdLFsicHJvZHVjdENhdGVnb3J5IixudWxsLG51bGxdLFsicHJvZHVjdE1hbnVmYWN0dXJlciIsbnVsbCxudWxsXSxbInN0b2NrTG9jYXRpb24iLG51bGwsbnVsbF0sWyJzdG9ja01hZ2F6aW5lIixudWxsLG51bGxdLFsic3RvY2tFZmZlY3RpdmVEYXRlIixudWxsLG51bGxdXQ%3D%3D"
    "&reportTitle=Stock%20Quantity%20by%20Sublocation%20In%20Units"
)

BACKORDER_DEMAND_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1789740640527/Report.json?format=jsonObject&data=stock"
    "&attrName=%23%23sale013"
    "&rowDimensions=~mJrNA2HAy0BpZmZmZmZmwMDAwMDAwJrNA2LAy0BpZmZmZmZmwMDAwMDAwJrNA2XAzP7AwMDAAcDAms0B_sDLQGlmZmZmZmbAwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAyOMDNAX3AwMDAwMDAms0BzcDM_sDAwMDAwMCauXN0b2NrT3JkZXJTaGlwcGluZ1NlcnZpY2XAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAzNcDM_sDAwMDAwMA"
    "&metrics=~m5rNBL6zVW5pdHMgcnN2ZFxub24gaGFuZMtAaWZmZmZmZsDAwMDAwMCazQS9tlVuaXRzIHJzdmRcbmJhY2sgb3JkZXLLQGlmZmZmZmbAwMDAwMDAms0Eu61Vbml0c1xucGFja2Vky0BpZmZmZmZmwMDAwMDAwJrZPXByb2R1Y3RSZW9yZGVyTGV2ZWxNYXhMYWJhbGxleWxsY2FwaWZhY2lsaXR5MTAwMzA4Q29uc29saWRhdGXAzP7AwMDAwMDAmr9wcm9kdWN0VXNlclVzZXIxMDAzMkNvbnNvbGlkYXRlsUxvdCBJRCB0byBQcm9kdWNlzP7AwMDAwMDAmr9wcm9kdWN0VXNlclVzZXIxMDAzMkNvbnNvbGlkYXRlwMz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzJDb25zb2xpZGF0ZatTdWJsb2NhdGlvbsz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMjhDb25zb2xpZGF0ZatEZXNjcmlwdGlvbsz-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzJDb25zb2xpZGF0ZaVCbGFua8z-wMDAwMDAwJq_cHJvZHVjdFVzZXJVc2VyMTAwMzNDb25zb2xpZGF0ZcDM_sDAwMDAwMCav3Byb2R1Y3RVc2VyVXNlcjEwMDMyQ29uc29saWRhdGWmVmVyaWZ5zP7AwMDAwMDA"
    "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fUlNWRCIsIlNUT0NLX0lURU1fUEFDS0VEIl0sbnVsbF0sWyJwcm9kdWN0UHJvZHVjdFVybCIsbnVsbCxudWxsXSxbInByb2R1Y3RDYXRlZ29yeSIsbnVsbCxudWxsXSxbInByb2R1Y3RNYW51ZmFjdHVyZXIiLG51bGwsbnVsbF0sWyJwcm9kdWN0U3VwcGxpZXIiLG51bGwsbnVsbF0sWyJzdG9ja09yZGVyT3JkZXJVcmwiLG51bGwsbnVsbF0sWyJzdG9ja09yZGVyT3JpZ2luIixudWxsLG51bGxdLFsic3RvY2tPcmRlck9yZGVyRGF0ZSIsIltudWxsLG51bGxdIixudWxsXSxbInN0b2NrT3JkZXJDdXN0b21lciIsbnVsbCxudWxsXSxbInN0b2NrTG9jYXRpb24iLG51bGwsbnVsbF1d"
    "&reportTitle=Backordered%20sales%20by%20order%20(SkD)"
)

# Finale's UI generated this as pivotTableStream (returns HTML, not JSON,
# when fetched directly) — swapped to pivotTable here, same fix already
# confirmed necessary and working for other reports saved from certain
# UI contexts. Each row pairs a finished/parent Product ID with one
# Component Product ID it's built from — used to classify every SKU as
# "built" (appears as a parent here) vs "purchased" (never does),
# confirmed 2026-10-01: purchased SKUs were incorrectly appearing in
# Stock Builds, which should only ever suggest things to BUILD.
PRODUCT_SALES_HISTORY_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1790374113747/Report.json?format=jsonObject&data=orderItem"
    "&attrName=%23%23sale011"
    "&rowDimensions=~n5rNAf7Ay0BzDMzMzMzNwMDAwMDAwJrNAdTAy0CBdmZmZmZmwMDAwAHAwJrM1cDLQGlmZmZmZmbAwMDAAcDAmszWwMtAcwzMzMzMzcDAwMDAwMCazQEFwMtAZ9AAAAAAAMDAwMDAwMCazLnAy0BzDMzMzMzNwMDAwMDAwJrMxMAAwMDAwMDAwJrMyMDLQGT0euFHrhTAwMDAwMDAmszKwMtAZPR64UeuFMDAwMDAwMCazNHAy0Bk9HrhR64UwMDAwMDAwJq0cHJvZHVjdFVzZXJVc2VyMTAwMjLAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAyM8DM_sDAwMDAwMCatHByb2R1Y3RVc2VyVXNlcjEwMDI0wMz-wMDAwMDAwJq0cHJvZHVjdFVzZXJVc2VyMTAwMjXAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAyNsDM_sDAwMDAwMA"
    "&metrics=~kZrNBaWoU3VidG90YWzLQGT0euFHrhTAwMDAwMDA"
    "&filters=W1sicHJvZHVjdFByb2R1Y3RVcmwiLCBudWxsLCBudWxsXSwgWyJwcm9kdWN0Q2F0ZWdvcnkiLCBudWxsLCBudWxsXSwgWyJvcmRlckN1c3RvbWVyIiwgbnVsbCwgbnVsbF0sIFsib3JkZXJPcmRlckRhdGUiLCAiW251bGwsbnVsbF0iLCBudWxsXSwgWyJvcmRlck9yaWdpbiIsIG51bGwsIG51bGxdLCBbIm9yZGVyVHlwZSIsIFsiU0FMRVNfT1JERVIiXSwgbnVsbF0sIFsib3JkZXJTdGF0dXMiLCBbIk9SREVSX0NPTVBMRVRFRCIsICJPUkRFUl9DUkVBVEVEIiwgIk9SREVSX0xPQ0tFRCJdLCBudWxsXV0%3D"
    "&reportTitle=Product%20sales%20history"
)

INVENTORY_VALUATION_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1790908079191/Report.json?format=jsonObject&data=stock"
    "&attrName=%23%23stock022"
    "&rowDimensions=~lJrNA0XAy0BjDMzMzMzNwMDAwAHAwJrNAf7Ay0Bz2AAAAAAAwMDAwMDAwJrNAdTAy0CF1AAAAAAAwMAAwMDAwJrNAgupU3RkXG5Qa25ny0BjDMzMzMzNwMDAwMDAwA"
    "&metrics=~l5rNBkPAzP7AwMDAwMDAms0EuKpVbml0c1xuUW9Iy0BjDMzMzMzNwMDAwMDAwJrNBLutVW5pdHNcblBhY2tlZMtAYwzMzMzMzcDAwMDAwMCazQS_rlVuaXRzXG5UcmFuc2l0y0BjDMzMzMzNwMDAwMDAwJrNBMCqVW5pdHNcbldJUMtAYwzMzMzMzcDAwMDAwMCazQTlr1RvdGFsXG5BdmcgY29zdMtAaWZmZmZmZsDAwMDAwMCazQTasVRvdGFsXG5JdGVtIHByaWNly0BpZmZmZmZmwMDAwMDAwA"
    "&styles=~gqtncm91cEhlYWRlcpYIwMDAwMCkYm9keZYIwMDAwMA"
    "&filters=W1sic3RvY2tUeXBlIixbIlNUT0NLX0lURU1fT05fSEFORCIsIlNUT0NLX0lURU1fSU5fVFJBTlNJVCIsIlNUT0NLX0lURU1fV0lQIiwiU1RPQ0tfSVRFTV9QQUNLRUQiXSxudWxsXSxbInByb2R1Y3RTdGF0dXMiLFsiUFJPRFVDVF9BQ1RJVkUiXSxudWxsXSxbInByb2R1Y3RQcm9kdWN0VXJsIixudWxsLG51bGxdLFsicHJvZHVjdENhdGVnb3J5IixudWxsLG51bGxdLFsicHJvZHVjdE1hbnVmYWN0dXJlciIsbnVsbCxudWxsXSxbInN0b2NrTG9jYXRpb24iLG51bGwsbnVsbF0sWyJzdG9ja0VmZmVjdGl2ZURhdGUiLG51bGwsbnVsbF1d"
    "&reportTitle=Inventory%20valuation%20by%20location%2C%20in%20units"
)

MASTER_PRODUCT_LIST_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1790969176651/Report.json?format=jsonObject&data=product"
    "&attrName=%23%23product001"
    "&rowDimensions=~3AAQms0CCMB_wMDAwMDAwJrNAf7Ay0Bz2AAAAAAAwMDAwMDAwJrNAeLAzP7AwMDAwMDAms0B48DM_sDAwMDAwMCazQHUwMtAgdwAAAAAAMDAAMDAwMCazQHNwMtAbdhR64UeuMDAwMDAwMCazQH1wMz-wMDAwMDAwJq7cHJvZHVjdFVuaXZlcnNhbFByb2R1Y3RDb2RlwMtAbJMzMzMzM8DAwMDAwMCa2SFwcm9kdWN0SW50ZXJuYXRpb25hbEFydGljbGVOdW1iZXLAy0BskzMzMzMzwMDAwMDAwJrNAgnAy0Bn0AAAAAAAwADAwMDAwJrNAXWoQXZnIGNvc3TLQGfQAAAAAADAAsDAwMDAms0B5cDLQGfQAAAAAADAwMDAwMDAms0B0cDM_sDAwMDAwMCatHByb2R1Y3RVc2VyVXNlcjEwMDA0wMz-wMDAwMDAwJqxcHJvZHVjdFVuaXRzVG90YWzAzP7AwMDAwMDAmrRwcm9kdWN0VXNlclVzZXIxMDAzM8DM_sDAwMDAwMA"
    "&filters=W1sicHJvZHVjdFN0YXR1cyIsWyJQUk9EVUNUX0FDVElWRSJdLG51bGxdLFsicHJvZHVjdENhdGVnb3J5IixudWxsLG51bGxdXQ%3D%3D"
    "&reportTitle=Master%20product%20list"
)

PRODUCT_BOM_REPORT_URL = (
    "https://app.finaleinventory.com/laballeyllc/doc/report/pivotTable/"
    "1790865378562/Report.json?format=jsonObject&data=productBom"
    "&attrName=%23%23other007"
    "&rowDimensions=~m5rNAf7Ay0BzDMzMzMzNwMDAwMDAwJrNAdTAzQH8wMDAwMDAwJq9cHJvZHVjdFBvdGVudGlhbEJ1aWxkUXVhbnRpdHm1UG90ZW50aWFsXG5CdWlsZFxuUXR5zP7AwMDAAMDAms0BdsAAwADAwMDAwJrNAcrAy0BjDMzMzMzNwADAwMDAwJrNAZ-2Q29tcG9uZW50IFxuUHJvZHVjdCBJRMtAcwzMzMzMzcDAwMDAwMCazQGDwM0B_MDAwMDAwMCa2T9wcm9kdWN0Qm9tUHJvZHVjdFN0b2NrUmVtYWluaW5nQWZ0ZXJSZXNlcnZhdGlvbnNDYXNlRXF1aXZhbGVudHO-Q29tcG9uZW50IHByb2R1Y3QgXG4gUmVtYWluaW5nzP7AwMDAwMDAms0Bd8DM_sDAAMDAwMCatHByb2R1Y3RVc2VyVXNlcjEwMDAwwMz-wMDAwMDAwJrNAXnAzP7AwMDAwMDA"
    "&filters=W1sicHJvZHVjdFByb2R1Y3RVcmwiLG51bGwsbnVsbF0sWyJwcm9kdWN0Qm9tUHJvZHVjdFVybCIsbnVsbCxudWxsXV0%3D"
    "&reportTitle=Product%20bill%20of%20materials"
)

# CONFIRMED (2026-09-18) against the complete real list of 681 distinct
# sublocation names in the account: this pattern separates all 13 known-
# excluded locations (Quality Hold "*-QH", Inventory Hold "*-IH", Do Not
# Inventory "*-DNI", Amazon FBA, Lab Room storage, Supplies, Receiving,
# Downpacking staging) from the 668 genuine pickable storage bins, with
# zero false positives or negatives against that full list. An allowlist
# by design: any NEW sublocation added later that doesn't match one of
# these three shapes is excluded by default (safer than silently
# counting an unknown location as real, pickable stock) — Casey will
# extend this pattern if a legitimate new location format shows up.
USABLE_SUBLOCATION_PATTERN = re.compile(
    r"^[A-Za-z]-\d+\.\d+$"      # e.g. "C-1.06"
    r"|^[A-Za-z]-BULK$"          # e.g. "D-BULK", "E-Bulk"
    r"|^[A-Za-z]\d+\.\d+$",      # e.g. "L1.01" (no hyphen — a distinct, confirmed-usable naming convention)
    re.IGNORECASE,
)


def is_usable_sublocation(name: str) -> bool:
    """Whether a sublocation represents real, pickable stock — see
    USABLE_SUBLOCATION_PATTERN above for how this was derived and
    verified."""
    return bool(USABLE_SUBLOCATION_PATTERN.match((name or "").strip()))


def fetch_usable_stock() -> dict[str, dict]:
    """Returns {product_id: {"usable_stock": float, "description": str,
    "lots": {lot_id: qty}, "lot_sublocations": {lot_id: {sublocation:
    qty}}}}, quantity summed only across sublocations that pass
    is_usable_sublocation() — excludes Quality Hold, Inventory Hold, Do
    Not Inventory, Amazon FBA stock, and staging/processing areas that
    aren't real, currently-pickable inventory. This is what makes "38.8
    units on hand" correctly read as "36.3 usable" when ~2.5 of those
    units are actually sitting in Quality Hold — the exact real example
    that started this feature.

    The per-lot breakdown (confirmed against real data, 2026-09-18) is
    what powers the same-lot fulfillment check: a single lot's stock is
    often split across several usable sublocations (e.g. one real lot,
    '5001997/21.1', appeared at 4 different bins for the same product),
    so this sums by lot ID across all of them, not just one row each.

    lot_sublocations preserves that same per-sublocation breakdown
    (rather than collapsing straight to a lot total) — added for the
    Shipping List's "where do I physically go to pick this" column.
    Deliberately a SEPARATE field alongside the existing flat lots
    dict, not a reshape of it — lots is already relied on, tested, and
    correct throughout the frontend's depletion/same-lot logic exactly
    as a flat {lot_id: qty} map; this only adds new information rather
    than risk that working code by changing its shape."""
    rows = fetch_finale_report(STOCK_BY_SUBLOCATION_REPORT_URL)
    stock: dict[str, dict] = {}
    for row in rows:
        sublocation = row.get("Sublocation")
        pid = row.get("Product ID")
        if not sublocation or not pid:
            continue
        pid = pid.strip()
        if pid not in stock:
            stock[pid] = {
                "usable_stock": 0.0, "description": row.get("Description") or "",
                "lots": {}, "lot_sublocations": {},
            }
        elif not stock[pid]["description"] and row.get("Description"):
            stock[pid]["description"] = row.get("Description")
        if not is_usable_sublocation(sublocation):
            continue
        qoh = row.get("Units\nQoH")
        if isinstance(qoh, (int, float)):
            stock[pid]["usable_stock"] += qoh
            lot_id = (row.get("Lot ID unprefixed") or "").strip()
            if lot_id:
                stock[pid]["lots"][lot_id] = stock[pid]["lots"].get(lot_id, 0.0) + qoh
                sub_map = stock[pid]["lot_sublocations"].setdefault(lot_id, {})
                sub_map[sublocation] = sub_map.get(sublocation, 0.0) + qoh
    return stock


def fetch_backorder_demand() -> dict[str, dict]:
    """Returns {product_id: {reorder_point_max, units_backorder,
    units_on_hand_reserved}}, aggregated across every order line for
    that product from the 'Backordered sales by order (SkD)' report.

    This report has the same hierarchical/grouped structure as the main
    Orders report (an order-header row, then product-detail rows
    beneath it, then a "TOTAL:" footer row) — we only need the
    product-detail rows here, so header/footer rows are simply skipped
    rather than needing the full stateful order-context parsing.

    No Origin-based filtering needed: drop-ship vendor orders (Biologix,
    GT, etc.) never enter Finale at all, confirmed directly — so every
    row in this report already represents real Lab Alley demand."""
    rows = fetch_finale_report(BACKORDER_DEMAND_REPORT_URL)
    demand: dict[str, dict] = {}
    for row in rows:
        pid = row.get("Product ID")
        if not pid or pid.strip() == "TOTAL:":
            continue
        pid = pid.strip()
        if pid not in demand:
            demand[pid] = {
                "reorder_point_max": None,
                "units_backorder": 0,
                "units_on_hand_reserved": 0,
            }
        entry = demand[pid]

        reorder = row.get("DrippingSprings\nreorder point max")
        if entry["reorder_point_max"] is None and isinstance(reorder, (int, float)):
            entry["reorder_point_max"] = reorder

        back = row.get("Units rsvd\nback order")
        if isinstance(back, (int, float)):
            entry["units_backorder"] += back

        onhand = row.get("Units rsvd\non hand")
        if isinstance(onhand, (int, float)):
            entry["units_on_hand_reserved"] += onhand
    return demand


CORE_QUEUE_USERS = {"Warehouse": "Warehouse", "Jerry": "Freight"}
WAREHOUSE_LOCATION_NAME = "Dripping Springs Warehouse"
FREIGHT_LOCATION_NAME = "Dripping Springs Freight"

TAG_DISPLAY_OVERRIDES: dict[str, str] = {}


def normalize_order_id(value) -> str:
    """Order IDs can come back as int or str depending on the source;
    normalize to a plain string for matching."""
    if value is None:
        return ""
    return str(value).strip()


def ss_items_for_order(order: dict) -> str:
    """JSON-encoded list of {sku, name, qty} for every real line item
    ShipStation has on this order. Originally captured just for the
    "Multiple products" fallback (Finale's own Orders report sometimes
    collapses a split/backordered order's distinct products into one
    summary row, so we can't tell which real SKUs were involved). Now
    also carries per-item quantity, since ShipStation's order data has
    it — this is what makes it possible to check "does this order
    actually need MORE units than we have usable stock for," not just
    "does some usable stock exist at all.\""""
    items = order.get("items") or []
    return json.dumps([
        {
            "sku": (item.get("sku") or "").strip(),
            "name": (item.get("name") or "").strip(),
            "qty": item.get("quantity") or 0,
        }
        for item in items
    ])


def order_weight_lbs(order: dict) -> float:
    """Order weight in pounds, regardless of what unit ShipStation
    reports it in. Confirmed against a real order (000406235): its
    internal notes literally state "W=451" and its weight object was
    {"value": 7216.0, "units": "ounces"} — 7216 / 16 = 451, confirming
    the ounces-to-pounds conversion lines up with Lab Alley's own
    packing paperwork. Used for measuring actual queue WORKLOAD, not
    just order count — a 500 lb order and a 2 lb order currently look
    identical if you only count orders."""
    weight = order.get("weight") or {}
    value = weight.get("value")
    units = (weight.get("units") or "").lower()
    if value is None:
        return 0.0
    if units == "pounds":
        return float(value)
    if units == "ounces":
        return float(value) / 16.0
    if units == "grams":
        return float(value) / 453.592
    # Unknown/unexpected unit — return 0 rather than silently reporting a
    # wrong number in an unfamiliar unit; worth noticing in totals if
    # this ever actually happens.
    return 0.0


def order_item_quantity(order: dict) -> int:
    """Total unit count across every line item on the order (sum of
    each item's quantity) — a rough measure of how much physical work
    one order represents, distinct from how many distinct SKUs it has."""
    items = order.get("items") or []
    return sum(int(item.get("quantity") or 0) for item in items)


def store_name_for_order(order: dict, store_name_by_id: dict[int, str]) -> str:
    """Which sales channel (Magento, Amazon, Walmart, etc.) this order
    came from, resolved from advancedOptions.storeId via
    client.list_stores()."""
    store_id = (order.get("advancedOptions") or {}).get("storeId")
    if store_id is None:
        return ""
    return store_name_by_id.get(store_id, f"(unknown store {store_id})")


def tags_for_order(order: dict, tag_name_by_id: dict[int, str]) -> str:
    """Comma-separated real tag name(s) for an order, applying any display
    override and trimming stray whitespace (ShipStation has at least one
    tag name with a trailing space — "ATX Freight "). An order can carry
    more than one tag (e.g. a split order needing both Warehouse and a
    drop-ship vendor), so this is deliberately not a single value. Kept
    as an informational field only — see the note above for why this is
    no longer what determines core_queue."""
    tag_ids = order.get("tagIds") or []
    names = [tag_name_by_id.get(tid, f"(unknown tag {tid})").strip() for tid in tag_ids]
    names = [TAG_DISPLAY_OVERRIDES.get(n, n) for n in names]
    return ", ".join(names)


def core_queue_for_order(order: dict, warehouse_id: int, freight_id: int) -> str:
    """The real classification: which Ship From Location this order is
    set to (order['advancedOptions']['warehouseId']) — 'Warehouse',
    'Freight', or '' (something else: a drop-ship vendor, unassigned, a
    hold, etc.). warehouse_id/freight_id come from
    client.get_warehouse_id(WAREHOUSE_LOCATION_NAME) /
    client.get_warehouse_id(FREIGHT_LOCATION_NAME), resolved once per run
    by the caller."""
    wh_id = (order.get("advancedOptions") or {}).get("warehouseId")
    if wh_id == warehouse_id:
        return "Warehouse"
    if wh_id == freight_id:
        return "Freight"
    return ""



def fetch_finale_report(url: str) -> list[dict]:
    """Runs a saved Finale report fresh via the Reporting API. This is the
    single most expensive step in either script (the Orders report has
    150k+ rows) — Finale's Reporting API has no way to ask for "just
    these N order IDs," so both the full historical pull and the
    live-queue pull have to download the whole thing and filter locally.

    Retries on transient connection failures (confirmed in production:
    Finale's server sometimes drops the connection mid-response while
    generating this large a report — "Remote end closed connection
    without response" — which has nothing to do with our request being
    wrong, just Finale occasionally timing out on a slow, heavy report)
    AND on 429 (rate limited) / 5xx (Finale's own server error) HTTP
    responses — confirmed in production (2026-09-22): Live Queue Pull and
    Stock Levels Pull running at the same scheduled time both hit
    Finale's reports API in close succession and got 429'd. The bug: a
    429 raises HTTPError, which wasn't in the retried exception list at
    all — so it fell straight through as an immediate crash regardless
    of how many attempts were configured, even after two earlier
    ConnectionErrors on the SAME run had already been retried correctly.
    Does NOT retry on other 4xx responses (401, 400, etc.) — those
    indicate a genuine problem (bad credentials, bad request) worth
    surfacing immediately rather than masking with a retry.
    """
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, auth=(FINALE_API_KEY, FINALE_API_SECRET), timeout=180)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == max_attempts:
                raise
            wait = 10 * attempt  # 10s, 20s, 30s
            print(f"  Finale request failed ({e.__class__.__name__}), "
                  f"retrying in {wait}s (attempt {attempt}/{max_attempts})...")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status != 429 and not (status is not None and 500 <= status < 600):
                raise  # a real problem (bad credentials, bad request, etc.) — surface it immediately
            if attempt == max_attempts:
                raise
            retry_after = None
            if e.response is not None:
                retry_after = e.response.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 30 * attempt  # 30s, 60s, 90s
            print(f"  Finale request failed (HTTP {status}), "
                  f"retrying in {wait}s (attempt {attempt}/{max_attempts})...")
            time.sleep(wait)


def fetch_finale_product_lines() -> dict[str, list[dict]]:
    """Pulls the Orders report and groups product-line rows by Order ID.
    This is Finale's ONLY job in this pipeline: static product
    attributes, looked up by Order ID. No status of any kind comes from
    here."""
    print("Pulling Orders report from Finale (product attributes only)...")
    rows = fetch_finale_report(ORDERS_REPORT_URL)
    print(f"  {len(rows)} order-line rows")
    by_order_id: dict[str, list[dict]] = {}
    for row in rows:
        oid = normalize_order_id(row.get("Order ID"))
        by_order_id.setdefault(oid, []).append(row)
    return by_order_id


# --- Supabase storage layer -------------------------------------------
#
# SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for both write
# paths below. The service role key bypasses Row Level Security by
# design (that's what makes it able to write at all) — it must only ever
# live in GitHub Actions Secrets or a local .env, never in anything
# served to a browser. The separate, read-only anon key used by the
# Netlify frontend is unrelated to this file entirely.
#
# Our row dicts use CSV-style keys ("Order ID", "CHEM TECH") for
# readability throughout the pipeline; Postgres columns are snake_case.
# This is the one place that translation happens.

COLUMN_TO_DB = {
    "Order ID": "order_id",
    "Order date": "order_date",
    "Order datetime": "order_datetime",
    "Product ID": "product_id",
    "Description": "description",
    "Category": "category",
    "CHEM": "chem",
    "CHEM TECH": "chem_tech",
    "LAB ROOM": "lab_room",
    "Downpack Product": "downpack",
    "HAZMAT": "hazmat",
    "SS Items": "ss_items",
    "Weight (lbs)": "weight_lbs",
    "Item Quantity": "item_quantity",
    "Store": "store_name",
    "Shipment ID": "shipment_id",
    "Ship date actual": "ship_date_actual",
    "Shipment status": "shipment_status",
    "Tags": "tags",
    "Core Queue": "core_queue",
}

SUPABASE_BATCH_SIZE = 500  # rows per insert call, to stay well under request-size limits


def get_supabase_client():
    """Lazily creates the Supabase client — only imports/requires the
    `supabase` package and env vars when a caller actually needs to write,
    so `--no-supabase` local test runs don't need either installed."""
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def to_db_rows(rows: list[dict], pulled_at: str) -> list[dict]:
    """Converts our CSV-style row dicts into snake_case dicts matching the
    Postgres schema, adding pulled_at to every row."""
    db_rows = []
    for row in rows:
        db_row = {"pulled_at": pulled_at}
        for csv_key, db_key in COLUMN_TO_DB.items():
            if csv_key in row:
                db_row[db_key] = row[csv_key]
        db_rows.append(db_row)
    return db_rows


def select_all_rows(client, table: str) -> list[dict]:
    """Reads an ENTIRE table, paginating past Supabase's default
    1000-row-per-request cap via .range(). CONFIRMED BUG, FIXED
    (2026-10-03): compute_turnover()'s read of turnover_snapshots used
    a plain .select("*").execute() with no pagination at all — silently
    truncated to the first 1000 rows, undercounting real products (1000
    shown instead of 1083 on day one) and set to get WORSE every day,
    since this table grows by roughly one row per product per day
    forever. Any future read of a table that could plausibly exceed
    1000 rows should go through this, not a bare .select("*")."""
    all_rows: list[dict] = []
    page_size = 1000
    start = 0
    while True:
        result = client.table(table).select("*").range(start, start + page_size - 1).execute()
        batch = result.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return all_rows


def insert_in_batches(client, table: str, db_rows: list[dict]) -> None:
    for i in range(0, len(db_rows), SUPABASE_BATCH_SIZE):
        batch = db_rows[i:i + SUPABASE_BATCH_SIZE]
        client.table(table).insert(batch).execute()
        print(f"  inserted rows {i+1}-{i+len(batch)} of {len(db_rows)} into {table}")


def append_to_snapshots(rows: list[dict], pulled_at: str) -> None:
    """The historical write: pure append, matching the append-only SQLite
    version this replaced. Nothing here is ever deleted or overwritten —
    the auto-migration concern that mattered for SQLite doesn't apply
    here, since the Postgres schema is fixed by schema.sql; adding a new
    column later means re-running an ALTER TABLE by hand once, not
    something the script needs to detect at runtime."""
    if not rows:
        print("No rows to append to snapshots.")
        return
    client = get_supabase_client()
    db_rows = to_db_rows(rows, pulled_at)
    insert_in_batches(client, "snapshots", db_rows)
    print(f"Appended {len(db_rows)} rows to Supabase 'snapshots' (pulled_at: {pulled_at})")


def replace_live_queue(rows: list[dict], pulled_at: str) -> None:
    """The live-queue write: unlike snapshots, this table represents
    "right now" only, so each run replaces the ENTIRE contents rather
    than appending — matching how live_queue.csv used to be overwritten
    wholesale every run."""
    client = get_supabase_client()
    # Delete every existing row first. Supabase requires a filter on
    # delete; id > 0 matches every row since id is an always-positive
    # identity column.
    client.table("live_queue").delete().gt("id", 0).execute()
    print("Cleared existing live_queue rows.")
    if not rows:
        print("No new rows to write to live_queue.")
        return
    db_rows = to_db_rows(rows, pulled_at)
    insert_in_batches(client, "live_queue", db_rows)
    print(f"Wrote {len(db_rows)} rows to Supabase 'live_queue' (pulled_at: {pulled_at})")


def fetch_product_categories() -> dict[str, str]:
    """Returns {product_id: category}, from Finale's "Master product
    list" report — confirmed (2026-10-03) a genuinely flat, catalog-
    wide listing (one row per product, no header/detail nesting unlike
    every other report used so far), with Category populated for 6060
    of 6195 products regardless of current order/sales activity. Built
    specifically to replace the Inventory Turnover tab's earlier
    category lookup, which only covered products with a CURRENT order
    in live_queue — a much narrower, activity-dependent subset."""
    rows = fetch_finale_report(MASTER_PRODUCT_LIST_REPORT_URL)
    categories = {}
    for row in rows:
        pid = row.get("Product ID")
        category = row.get("Category")
        if pid and category:
            categories[pid.strip()] = category
    return categories


def fetch_built_sku_set() -> set[str]:
    """Returns the set of every Product ID that is BUILT in-house,
    determined from the Product Bill of Materials report. Confirmed
    structure (2026-10-01): a header row per built/parent product (only
    "Product ID" and "Potential Build Qty" populated), immediately
    followed by one detail row per component it's built from (header's
    own "Product ID" is null on these; the real data is under
    "Component Product ID"). A SKU is built if it EVER appears as a
    non-null "Product ID" header row anywhere in this report; every
    other SKU — including every component-only SKU, and any SKU that
    never appears in this report at all — defaults to purchased, the
    agreed behavior for SKUs with no BOM relationship.

    This exists specifically to stop purchased raw materials from
    showing up in Stock Builds, which should only ever suggest things
    to BUILD, not things to buy (confirmed bug, 2026-10-01)."""
    rows = fetch_finale_report(PRODUCT_BOM_REPORT_URL)
    built = set()
    for row in rows:
        pid = row.get("Product ID")
        if pid:
            built.add(pid.strip())
    return built


def fetch_inventory_valuation() -> dict[str, dict]:
    """Returns {product_id: {"units_on_hand": float, "total_value": float,
    "fba_units_on_hand": float, "fba_total_value": float}}, from the
    "Inventory valuation by location, in units" report. Confirmed
    structure (2026-10-01): a header row per location (only "Location"
    populated), followed by one detail row per product at that
    location (that row's own "Location" is null). The same product can
    appear multiple times, once per location it has stock in — summed
    here across every non-Amazon location into the main figures, and
    separately across every Amazon location into the fba_ figures, per
    Casey's call to track Amazon FBA stock but report it separately
    rather than merge it in or drop it (it isn't held at Lab Alley's
    own physical location). "Total Avg cost" is Finale's own weighted-
    average-cost valuation for that row — used directly rather than
    recomputed from Average cost x QoH, since this project confirmed
    some products have no Average cost configured at all (null), yet
    Finale still reports a correct Total Avg cost of 0 for them rather
    than a wrong one — recomputing ourselves from a null cost would
    just rediscover the same gap less reliably.

    "Total Item price" (a separate, apparently sales-price-based field,
    confirmed populated even when cost is null) is deliberately NOT
    used here — mixing a sales-price fallback into a cost-based
    valuation would misrepresent what the number means."""
    rows = fetch_finale_report(INVENTORY_VALUATION_REPORT_URL)
    result: dict[str, dict] = {}
    current_location = None
    for row in rows:
        loc = row.get("Location")
        if loc:
            current_location = loc
            continue
        pid = row.get("Product ID")
        if not pid:
            continue
        pid = pid.strip()
        qoh = row.get("Units\nQoH") or 0.0
        value = row.get("Total\nAvg cost") or 0.0
        if pid not in result:
            result[pid] = {
                "units_on_hand": 0.0, "total_value": 0.0,
                "fba_units_on_hand": 0.0, "fba_total_value": 0.0,
            }
        is_fba = current_location is not None and "amazon" in current_location.lower()
        if is_fba:
            result[pid]["fba_units_on_hand"] += qoh
            result[pid]["fba_total_value"] += value
        else:
            result[pid]["units_on_hand"] += qoh
            result[pid]["total_value"] += value
    return result


def fetch_product_sales_by_date(start_date: str, end_date: str) -> dict[str, dict[str, dict]]:
    """Returns {product_id: {date (YYYY-MM-DD): {"units_sold": float,
    "dollars_sold": float}}} for real orders in [start_date, end_date]
    (inclusive), from Finale's "Product sales history" report. Broken
    down BY DATE (not just a single total) specifically so the four
    turnover windows (week/month/quarter/year) can each sum the right
    date range from ONE fetch — no extra Finale calls needed per
    window, since every smaller window's sales are a subset of this
    same date-keyed data.

    Confirmed structure (2026-10-03): a header row per Product ID (only
    "Product ID" populated), then a header row per Order date (format
    "M/D/YYYY", e.g. "9/5/2026" — converted to YYYY-MM-DD here for
    correct string comparison against window boundaries), then one
    detail row per Order ID with that line's Quantity/Subtotal — same
    nested header/detail tracking pattern as BOM and valuation, just
    one level deeper. "TOTAL:" rows are Finale's own subtotal rows for
    each date group, deliberately skipped (not real line items).

    FBA-bound products are tracked under their own distinct SKU
    (confirmed directly, e.g. "EAP190-1L- FBA" is a completely separate
    Product ID from "EAP190-1L", consistently between this report and
    inventory valuation) — so no separate location/channel join is
    needed here at all; summing by Product ID already keeps FBA and
    Dripping Springs sales naturally apart, the same way
    turnover_snapshots already does for inventory position."""
    date_range_json = json.dumps([start_date, end_date])
    filters = [
        ["productProductUrl", None, None],
        ["productCategory", None, None],
        ["orderCustomer", None, None],
        ["orderOrderDate", date_range_json, None],
        ["orderOrigin", None, None],
        ["orderType", ["SALES_ORDER"], None],
        ["orderStatus", ["ORDER_COMPLETED", "ORDER_CREATED", "ORDER_LOCKED"], None],
    ]
    filters_b64 = base64.b64encode(json.dumps(filters).encode("utf-8")).decode("ascii")
    filters_param = urllib.parse.quote(filters_b64, safe="")
    url = re.sub(r"&filters=[^&]+", f"&filters={filters_param}", PRODUCT_SALES_HISTORY_REPORT_URL)

    rows = fetch_finale_report(url)
    totals: dict[str, dict[str, dict]] = {}
    current_product = None
    current_date = None
    bad_date_examples: list[str] = []
    rows_skipped_no_date = 0
    for row in rows:
        pid = row.get("Product ID")
        if pid and pid != "--":
            current_product = pid.strip()
            current_date = None  # a new product always starts a fresh date group
            continue
        raw_order_date = row.get("Order date")
        if raw_order_date:
            try:
                current_date = datetime.strptime(raw_order_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                current_date = None
                if len(bad_date_examples) < 3:
                    bad_date_examples.append(str(raw_order_date))
            continue
        order_id = row.get("Order ID")
        if not order_id or order_id == "TOTAL:" or not current_product:
            continue
        if not current_date:
            rows_skipped_no_date += 1
            continue
        # Explicit isinstance checks, not `or 0` -- confirmed (2026-10-03)
        # some rows have a real Order ID but a blank-space string
        # Quantity/Subtotal, which `or 0` doesn't catch since a non-empty
        # string is truthy in Python.
        raw_qty = row.get("Quantity")
        raw_subtotal = row.get("Subtotal")
        qty = raw_qty if isinstance(raw_qty, (int, float)) else 0
        subtotal = raw_subtotal if isinstance(raw_subtotal, (int, float)) else 0
        product_totals = totals.setdefault(current_product, {})
        day_totals = product_totals.setdefault(current_date, {"units_sold": 0.0, "dollars_sold": 0.0})
        day_totals["units_sold"] += qty
        day_totals["dollars_sold"] += subtotal
    # This parser has only been verified against data shaped like what
    # Finale returned in testing. If the Order date format ever changes,
    # the failure mode would be sales silently reading as zero for
    # everything -- so make that impossible to miss in the run log
    # rather than quiet.
    if rows_skipped_no_date or bad_date_examples:
        print(f"  WARNING: {rows_skipped_no_date} sales rows were skipped because no parseable "
              f"'Order date' header preceded them (unparseable date examples: {bad_date_examples}). "
              f"Sales totals below may be UNDERCOUNTED -- check the Order date format.")
    return totals


TURNOVER_WINDOWS = {"week": 7, "month": 30, "quarter": 90, "year": 365}  # each builds up to its own max, then rolls forward


FBA_SKU_PATTERN = re.compile(r"\bFBA\b", re.IGNORECASE)


def is_fba_sku(product_id: str) -> bool:
    """Amazon FBA products have their own SKUs ("EAP190-1L- FBA"). Every
    SKU belongs to exactly one turnover group: Amazon FBA (these) or
    Dripping Springs (everything else). Mirrored in SQL as a whole-word,
    case-insensitive match on FBA (turnover_totals, kpi_summary) -- keep
    the two in step."""
    return bool(FBA_SKU_PATTERN.search(product_id or ""))


def _unit_costs_by_product(snapshots: list[dict]) -> dict[str, list[tuple[str, float]]]:
    """Per product, the (date, unit cost) of every snapshot day it had
    stock: (total_value + fba_total_value) / (units + fba_units)."""
    out: dict[str, list[tuple[str, float]]] = {}
    for r in snapshots:
        units = (r.get("units_on_hand") or 0) + (r.get("fba_units_on_hand") or 0)
        value = (r.get("total_value") or 0) + (r.get("fba_total_value") or 0)
        if units > 0 and value > 0:
            out.setdefault(r["product_id"], []).append((r["pulled_at"], value / units))
    for v in out.values():
        v.sort()
    return out


def _cost_on(costs: list[tuple[str, float]], day: str):
    """Unit cost on `day`: that day's snapshot, else the most recent one
    before it, else the earliest one after it. None if never stocked."""
    if not costs:
        return None
    before = [c for d, c in costs if d <= day]
    return before[-1] if before else costs[0][1]


def write_product_sales_daily(client, rows: list[dict]) -> None:
    for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
        client.table("product_sales_daily").upsert(rows[i:i + SUPABASE_BATCH_SIZE],
                                                   on_conflict="sale_date,product_id").execute()
    print(f"Wrote {len(rows)} product-day sales rows (units, dollars, COGS) to product_sales_daily.")


def compute_turnover(pulled_at: str) -> list[dict]:
    """Inventory turnover per product for four rolling windows (week /
    month / quarter / year, TURNOVER_WINDOWS), each building up from the
    first snapshot day until it reaches its full length.

    ONE definition, shared with the KPI Summary page (2026-10-08, agreed
    with Casey): this job is the only writer of daily sales and COGS
    (product_sales_daily), and the database function turnover_totals()
    computes group totals from the same data with the same math, so
    Ops Live's cards, its per-product table, and the KPI page agree.

      dollar turnover = COGS / average inventory value at cost, annualized
      unit turnover   = units sold / average units on hand, annualized
      COGS            = units sold x that day's unit cost (snapshot value
                        / units; nearest earlier snapshot if none that day)
      average         = sum over the window's snapshot days / number of
                        snapshot days (a product absent on a day counts
                        as zero that day, not skipped)
      annualized      = x 365 / calendar days in the window
      inventory       = all locations (Dripping Springs + Amazon)
      group           = Amazon FBA if the SKU contains "FBA", else
                        Dripping Springs (is_fba_sku)

    Previously dollar turnover divided SELLING price by inventory at
    COST, which overstated it, and averages divided by the number of
    days a product appeared, which overstated inventory for products
    that were out of stock part of the window."""
    client = get_supabase_client()
    snapshots = select_all_rows(client, "turnover_snapshots")
    if not snapshots:
        print("No turnover_snapshots history yet — nothing to compute.")
        return []

    all_dates = sorted(set(r["pulled_at"] for r in snapshots))
    end_date = all_dates[-1]
    earliest_date = all_dates[0]
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    widest_days = max(TURNOVER_WINDOWS.values())
    widest_cutoff = (end_dt - timedelta(days=widest_days - 1)).strftime("%Y-%m-%d")
    widest_start = max(earliest_date, widest_cutoff)
    widest_snapshots = [r for r in snapshots if r["pulled_at"] >= widest_start]

    by_product: dict[str, list[dict]] = {}
    for row in widest_snapshots:
        by_product.setdefault(row["product_id"], []).append(row)

    print(f"Pulling Product sales history from Finale for {widest_start} to {end_date} (widest window needed)...")
    sales_by_date = fetch_product_sales_by_date(widest_start, end_date)
    print(f"  {len(sales_by_date)} products with sales in this period")

    # Daily sales with COGS, written once for every consumer.
    costs = _unit_costs_by_product(snapshots)
    daily_rows = []
    cogs_by_product_date: dict[str, dict[str, float]] = {}
    for pid, dates in sales_by_date.items():
        for day, v in dates.items():
            if day < widest_start or day > end_date:
                continue
            units = float(v.get("units_sold") or 0)
            cost = _cost_on(costs.get(pid, []), day)
            cogs = units * cost if cost is not None else 0.0
            cogs_by_product_date.setdefault(pid, {})[day] = cogs
            daily_rows.append({
                "sale_date": day, "product_id": pid, "is_fba": is_fba_sku(pid),
                "units_sold": units, "dollars_sold": float(v.get("dollars_sold") or 0),
                "unit_cost": cost, "cogs": cogs, "cost_known": cost is not None,
            })
    if daily_rows:
        # The whole window is rewritten every day on purpose: the KPI page
        # reads these stored rows while this job uses the fresh pull, and
        # both must see the same sales for the numbers to match exactly.
        write_product_sales_daily(client, daily_rows)

    print("Pulling product categories from Finale (Master product list)...")
    categories = fetch_product_categories()
    print(f"  {len(categories)} products with a category on file")

    results = []
    all_pids = set(by_product) | set(sales_by_date)
    for window_name, window_days in TURNOVER_WINDOWS.items():
        cutoff = (end_dt - timedelta(days=window_days - 1)).strftime("%Y-%m-%d")
        window_start = max(earliest_date, cutoff)
        snap_days = len([d for d in all_dates if window_start <= d <= end_date])
        cal_days = (end_dt - datetime.strptime(window_start, "%Y-%m-%d")).days + 1
        annualize = 365.0 / cal_days if cal_days > 0 else 0
        window_desc = "still building up" if window_start == earliest_date and cal_days < window_days else "full window"
        print(f"  [{window_name}] {window_start} to {end_date} ({snap_days} snapshot days, {cal_days} calendar days, {window_desc})")

        for pid in all_pids:
            window_snapshots = [r for r in by_product.get(pid, []) if r["pulled_at"] >= window_start]
            sold = {d: v for d, v in sales_by_date.get(pid, {}).items() if window_start <= d <= end_date}
            if not window_snapshots and not sold:
                continue
            n = max(1, snap_days)
            avg_units = sum(r["units_on_hand"] or 0 for r in window_snapshots) / n
            avg_value = sum(r["total_value"] or 0 for r in window_snapshots) / n
            avg_fba_units = sum(r["fba_units_on_hand"] or 0 for r in window_snapshots) / n
            avg_fba_value = sum(r["fba_total_value"] or 0 for r in window_snapshots) / n
            units_sold = sum(float(v["units_sold"] or 0) for v in sold.values())
            dollars_sold = sum(float(v["dollars_sold"] or 0) for v in sold.values())
            cogs = sum(c for d, c in cogs_by_product_date.get(pid, {}).items() if window_start <= d <= end_date)
            total_units, total_value = avg_units + avg_fba_units, avg_value + avg_fba_value
            results.append({
                "computed_at": pulled_at,
                "product_id": pid,
                "category": categories.get(pid),  # None when Finale has no category on file -- shown as "Uncategorized"
                "window_name": window_name,  # NOT "window": reserved word in PostgreSQL
                "days_elapsed": snap_days,
                "calendar_days": cal_days,
                "group_name": "fba" if is_fba_sku(pid) else "main",
                "avg_units_on_hand": avg_units,
                "avg_value_on_hand": avg_value,
                "avg_fba_units_on_hand": avg_fba_units,
                "avg_fba_value_on_hand": avg_fba_value,
                "units_sold": units_sold,
                "dollars_sold": dollars_sold,
                "cogs": cogs,
                # None (not 0) when there's no inventory to divide by -- undefined, not zero.
                "turnover_units": (units_sold * annualize / total_units) if total_units > 0 else None,
                "turnover_dollars": (cogs * annualize / total_value) if total_value > 0 else None,
            })
    return results


def write_turnover_computed(results: list[dict]) -> None:
    """Replaces turnover_computed with the new results -- the CURRENT best
    computation, not a historical log (the real history lives in
    turnover_snapshots, which this is derived from).

    CONFIRMED FLAW, FIXED (2026-10-06): this used to delete the whole
    table FIRST and insert after, so any failure partway (e.g. a missing
    column) left the table EMPTY and the dashboard showing nothing --
    "no days collected, everything uncategorized, no turnover" -- until
    someone found the cause and re-ran it. Now the new rows go in FIRST
    and the old rows are removed only after every batch has succeeded; if
    an insert fails, the partial new rows are rolled back and the previous
    good data stays in place. An empty result also leaves the table alone
    rather than wiping it."""
    client = get_supabase_client()
    if not results:
        print("No turnover results to write -- leaving the existing turnover_computed rows untouched.")
        return
    prev = client.table("turnover_computed").select("id").order("id", desc=True).limit(1).execute().data
    old_max_id = prev[0]["id"] if prev else 0
    try:
        insert_in_batches(client, "turnover_computed", results)
    except Exception:
        client.table("turnover_computed").delete().gt("id", old_max_id).execute()
        print("Insert FAILED -- rolled back the partial new rows; the previous turnover_computed data is unchanged.")
        raise
    client.table("turnover_computed").delete().lte("id", old_max_id).execute()
    print(f"Wrote {len(results)} turnover computations to Supabase (replaced the previous set).")


def build_stock_levels() -> list[dict]:
    """Combines usable stock (Stock Quantity by Sublocation report) with
    backorder demand and reorder point max (Backordered sales by order
    report) into one row per product. A product only needs to appear in
    ONE of the two sources to show up here — e.g. a product with zero
    usable stock anywhere still needs its backorder demand visible, and
    a product with plenty of stock but no current backorders still
    needs its usable-stock number visible."""
    print("Pulling usable stock from Finale (Stock Quantity by Sublocation)...")
    stock = fetch_usable_stock()
    print(f"  {len(stock)} products with stock recorded somewhere")

    print("Pulling backorder demand from Finale (Backordered sales by order)...")
    demand = fetch_backorder_demand()
    print(f"  {len(demand)} products with backorder demand")

    print("Pulling built-SKU classification from Finale (Product bill of materials)...")
    built_skus = fetch_built_sku_set()
    print(f"  {len(built_skus)} products confirmed built in-house (everything else treated as purchased)")

    all_pids = set(stock.keys()) | set(demand.keys())
    rows = []
    for pid in all_pids:
        s = stock.get(pid, {})
        d = demand.get(pid, {})
        rows.append({
            "product_id": pid,
            "description": s.get("description", ""),
            "usable_stock": s.get("usable_stock", 0.0),
            "reorder_point_max": d.get("reorder_point_max"),
            "units_backorder": d.get("units_backorder", 0),
            "units_on_hand_reserved": d.get("units_on_hand_reserved", 0),
            "lots": json.dumps(s.get("lots", {})),
            "lot_sublocations": json.dumps(s.get("lot_sublocations", {})),
            "is_built": pid in built_skus,
        })
    return rows


def replace_stock_levels(rows: list[dict], pulled_at: str) -> None:
    """Product-level stock/demand snapshot — like live_queue, this
    represents "right now" only, so each run replaces the entire table
    rather than appending."""
    client = get_supabase_client()
    client.table("stock_levels").delete().gt("id", 0).execute()
    print("Cleared existing stock_levels rows.")
    if not rows:
        print("No new rows to write to stock_levels.")
        return
    db_rows = [{**row, "pulled_at": pulled_at} for row in rows]
    insert_in_batches(client, "stock_levels", db_rows)
    print(f"Wrote {len(db_rows)} rows to Supabase 'stock_levels' (pulled_at: {pulled_at})")


# --- Health snapshot logging (for the dashboard's Build/Aging/Volume
# health indicators — the Aging and Volume indicators need historical
# data to compare against, which doesn't exist yet; this is what starts
# building that history, one snapshot per pull run). ---

CHICAGO_TZ = ZoneInfo("America/Chicago")
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def parse_shipstation_naive(naive_str: str):
    """Parses a raw timestamp string exactly as ShipStation's API
    returns it — these are ALWAYS Pacific time, regardless of account
    settings. CONFIRMED BUG, FIXED (2026-09-23): verified directly
    against a real order (raw createDate 15:12:58 matched ShipStation's
    own UI showing 17:12 Central, an exact 2-hour offset, confirmed to
    the second). Before this fix, every order's raw timestamp was
    incorrectly parsed AS IF it were already Central time, which
    silently overstated every order's age by up to 2 hours —  pushing
    orders past the 24h/48h SLA thresholds earlier than they truly
    should have, project-wide. Mirrors the frontend's
    parseShipStationNaive (site/index.html); verified against the same
    real reference point and DST edge cases as that JS version.

    Returns the correct real-world instant as a timezone-aware UTC
    datetime — everything downstream (business-hours math, the
    production shift window) correctly converts that instant to
    Central time from there, since that's where the warehouse actually
    is; those functions were already correct and did not need to
    change."""
    if not naive_str:
        return None
    try:
        base = naive_str.split(".")[0]
        naive_dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    pacific_dt = naive_dt.replace(tzinfo=PACIFIC_TZ)
    return pacific_dt.astimezone(timezone.utc)


def order_age_hours(naive_str: str) -> float:
    """Real elapsed hours since the order's ShipStation Order Date --
    weekends and nights INCLUDED, exactly like ShipStation's own "Age"
    column, because that is the number the rest of the company sees and
    uses. CHANGED 2026-10-06: this used to be a "business hours" figure
    that excluded Saturday and Sunday entirely (the 48h clock paused
    all weekend), so every threshold derived from it -- 24h at-risk, 48h
    overdue, Build % -- disagreed with what everyone else was reading
    in ShipStation. Mirrors orderAgeHours() in site/index.html."""
    start = parse_shipstation_naive(naive_str)
    if not start:
        return 0.0
    seconds = (datetime.now(timezone.utc) - start).total_seconds()
    return seconds / 3600.0 if seconds > 0 else 0.0


# --- Off-hours order pile-up tracking (mirrors currentOffHoursWindow() /
# countOffHoursOrders() in site/index.html). Production shift: Mon-Fri,
# 8am-4pm Central, confirmed directly. ---

SHIFT_START_HOUR = 8
SHIFT_END_HOUR = 16


def _chicago_time_on_date(day_dt: datetime, hour: int) -> datetime:
    """Given any UTC instant, returns hour:00 Chicago time on that SAME
    Chicago calendar day, as a true UTC instant. Python's zoneinfo
    resolves this correctly natively (no iterative correction needed,
    unlike the JS version)."""
    chicago_day = day_dt.astimezone(CHICAGO_TZ)
    naive = chicago_day.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=None)
    return naive.replace(tzinfo=CHICAGO_TZ).astimezone(timezone.utc)


def _is_production_weekday(dt: datetime) -> bool:
    return dt.astimezone(CHICAGO_TZ).weekday() < 5  # Mon=0 ... Fri=4


def _most_recent_shift_end(now: datetime) -> datetime:
    cursor = now
    for _ in range(14):
        if _is_production_weekday(cursor):
            shift_end = _chicago_time_on_date(cursor, SHIFT_END_HOUR)
            if shift_end <= now:
                return shift_end
        cursor = cursor - timedelta(days=1)
    return None


def _next_shift_start_after(after: datetime) -> datetime:
    cursor = after
    for _ in range(14):
        if _is_production_weekday(cursor):
            shift_start = _chicago_time_on_date(cursor, SHIFT_START_HOUR)
            if shift_start > after:
                return shift_start
        cursor = cursor + timedelta(days=1)
    return None


def current_off_hours_window(now: datetime = None) -> dict:
    """The off-hours order pile-up window: starts growing the moment a
    shift ends, freezes the instant the next shift starts, resets at
    the next shift-end. Mirrors currentOffHoursWindow() exactly —
    tested against the same scenarios as that JS version."""
    now = now or datetime.now(timezone.utc)
    start = _most_recent_shift_end(now)
    next_start = _next_shift_start_after(start)
    end = min(now, next_start)
    gap_hours = (next_start - start).total_seconds() / 3600.0
    gap_type = "weekend" if gap_hours > 24 else "overnight"
    return {"start": start, "end": end, "gap_type": gap_type}


def count_off_hours_orders(rows: list[dict], window: dict) -> int:
    """Orders currently in the actionable queue whose Order datetime
    falls within the given off-hours window."""
    scoped = [
        r for r in rows
        if r.get("Core Queue") in ("Warehouse", "Freight", "Both")
        and r.get("Shipment status") != "on_hold"
    ]
    by_order: dict[str, list[dict]] = {}
    for r in scoped:
        by_order.setdefault(r["Order ID"], []).append(r)
    count = 0
    for oid, lines in by_order.items():
        dt = parse_shipstation_naive(lines[0].get("Order datetime"))
        if dt and window["start"] <= dt <= window["end"]:
            count += 1
    return count


def _is_case_sku(sku: str) -> bool:
    """All Lab Alley case SKUs end in 'CS', nothing ever follows it —
    confirmed directly. Mirrors isCaseSku() in site/index.html."""
    return sku.endswith("CS")


def _pair_sku_for(sku: str) -> str:
    """Case -> single: strip trailing 'CS'. Single -> case: append 'CS'.
    1 case = 4 singles, universally. Mirrors pairSkuFor()."""
    return sku[:-2] if _is_case_sku(sku) else sku + "CS"


def _build_working_stock(stock: dict[str, dict]) -> dict[str, dict]:
    """Deep, mutable copy of stock, split per SKU into {lots: {lotId:
    qty}, untracked: qty} — mirrors buildWorkingStock() in
    site/index.html. untracked covers usable stock with no lot ID
    (confirmed against real data: exclusively packaging/supplies, never
    actual order-able chemicals)."""
    working: dict[str, dict] = {}
    for sku, s in stock.items():
        lots = dict(s.get("lots") or {})
        lots_sum = sum(lots.values())
        usable = s.get("usable_stock", 0.0) or 0.0
        working[sku] = {
            "lots": lots,
            "untracked": max(0.0, usable - lots_sum),
        }
    return working


def _entry_total(entry: dict | None) -> float:
    if not entry:
        return 0.0
    return sum(entry["lots"].values()) + entry["untracked"]


def _deplete_from(entry: dict, qty: float) -> None:
    """Removes qty units from an entry's lots then untracked, mutating
    in place. Caller guarantees qty <= _entry_total(entry). Lot order
    doesn't matter here (unlike the frontend) — this function only
    needs the correct TOTAL depleted for shortage/build-burden purposes,
    not which specific lot a picker would choose, so it skips the FIFO
    lot-ordering the JS version needs for its Same Lot display column."""
    remaining = qty
    for lot_id in list(entry["lots"].keys()):
        if remaining <= 0:
            break
        take = min(remaining, entry["lots"][lot_id])
        entry["lots"][lot_id] -= take
        remaining -= take
    if remaining > 0:
        take = min(remaining, entry["untracked"])
        entry["untracked"] -= take


def _claim_stock(sku: str, needed: float, working: dict[str, dict]) -> tuple[float, bool]:
    """Checks availability AND, if satisfiable, depletes the mutable
    working stock — mirrors claimStock() in site/index.html (the fix
    for the real bug Casey found: multiple orders competing for the
    same limited stock were each checked independently against the same
    static figure, so e.g. 4 orders needing 1 unit each with only 2 in
    stock ALL showed as fulfillable). Orders must be processed in
    allocation-priority order by the caller. Returns (usable, is_blocked)."""
    pair_sku = _pair_sku_for(sku)
    direct = working.setdefault(sku, {"lots": {}, "untracked": 0.0})
    pair = working.setdefault(pair_sku, {"lots": {}, "untracked": 0.0})

    direct_total = _entry_total(direct)
    pair_total = _entry_total(pair)
    pair_converted = (pair_total // 4) if _is_case_sku(sku) else (pair_total * 4)
    total_usable = direct_total + pair_converted

    if total_usable < needed:
        return total_usable, True  # blocked — deplete nothing

    from_direct = min(needed, direct_total)
    _deplete_from(direct, from_direct)
    still_needed = needed - from_direct

    if still_needed > 0:
        pair_units_needed = (still_needed * 4) if _is_case_sku(sku) else (-(-still_needed // 4))  # ceil div
        _deplete_from(pair, min(pair_units_needed, pair_total))

    return total_usable, False


def compute_health_snapshot(rows: list[dict], stock: dict[str, dict]) -> dict:
    """Server-side port of the frontend's buildShippingList() /
    buildProductionList() logic (site/index.html) for a periodic history
    snapshot. The Supabase publishable key is deliberately read-only, so
    the frontend can never write these snapshots itself; this is why the
    computation has to be duplicated here rather than shared.

    IMPORTANT: this MUST stay logically consistent with the JS version.
    If the frontend's shortage/pickability logic changes, this needs a
    matching update, or Build Health's live number and its own logged
    history will silently drift apart. There is deliberately no shared
    source of truth between them (JS runs in the browser, this runs in
    GitHub Actions) — this comment is the closest thing to one.

    CONFIRMED DRIFT HAS ALREADY HAPPENED TWICE (2026-09-18):
      1. The Build % formula was redefined on the frontend and this
         function wasn't updated to match at the same time.
      2. This function checked every order independently against a
         static stock snapshot, the same cross-order depletion bug
         found and fixed on the frontend (see _claim_stock above) —
         it just hadn't been noticed here yet, since it doesn't show
         up as visibly in a single aggregate percentage the way it did
         in a per-order Pickable/Blocked table.
    Both fixed here; if this happens a third time, drift between the two
    implementations is the first thing to check.

    `rows` is the same shape build_rows() produces (Title Case keys,
    pre-to_db_rows). `stock` is {product_id: {usable_stock,
    reorder_point_max, units_backorder, lots}} as returned by
    build_stock_levels(), keyed by product_id.
    """
    scoped = [
        r for r in rows
        if r.get("Core Queue") in ("Warehouse", "Freight", "Both")
        and r.get("Shipment status") != "on_hold"
    ]

    by_order: dict[str, list[dict]] = {}
    for r in scoped:
        by_order.setdefault(r["Order ID"], []).append(r)

    # Allocation priority order — channel priority (Amazon/Walmart order
    # number prefix), then FIFO — matching the frontend exactly, so
    # whichever order has the strongest claim depletes stock first.
    order_items = sorted(
        by_order.items(),
        key=lambda kv: (
            0 if (kv[0].upper().startswith("AMZN") or kv[0].upper().startswith("WMT")) else 1,
            kv[1][0].get("Order datetime") or "",
        ),
    )

    working = _build_working_stock(stock)

    queue_count = len(by_order)
    warehouse_count = 0
    freight_count = 0
    both_count = 0
    total_units = 0
    aged_24h = 0
    aged_48h = 0
    bucket_0_24h = 0
    bucket_24_48h = 0
    bucket_48_72h = 0
    bucket_72h_plus = 0
    shortage_overdue_count = 0
    blocked_by_sku: dict[str, dict] = {}

    for oid, lines in order_items:
        first = lines[0]
        total_units += int(first.get("Item Quantity") or 0)

        core_queue = first.get("Core Queue")
        if core_queue == "Warehouse":
            warehouse_count += 1
        elif core_queue == "Freight":
            freight_count += 1
        elif core_queue == "Both":
            both_count += 1

        hours = order_age_hours(first.get("Order datetime"))
        is_overdue = hours >= 48
        if is_overdue:
            aged_48h += 1
        elif hours >= 24:
            aged_24h += 1

        if hours < 24:
            bucket_0_24h += 1
        elif hours < 48:
            bucket_24_48h += 1
        elif hours < 72:
            bucket_48_72h += 1
        else:
            bucket_72h_plus += 1

        try:
            ss_items = json.loads(first.get("SS Items") or "[]")
        except (json.JSONDecodeError, TypeError):
            ss_items = []
        qty_by_sku: dict[str, float] = {}
        for item in ss_items:
            sku = (item.get("sku") or "").strip()
            if sku:
                qty_by_sku[sku] = qty_by_sku.get(sku, 0) + (item.get("qty") or 0)

        distinct_skus = {(l.get("Product ID") or "").strip() for l in lines}
        distinct_skus.discard("")
        distinct_skus.discard("Multiple products")
        has_collapsed = any((l.get("Product ID") or "").strip() == "Multiple products" for l in lines)
        if has_collapsed and ss_items:
            distinct_skus |= {s for s in qty_by_sku if s}

        order_is_blocked = False
        for sku in distinct_skus:
            needed = qty_by_sku.get(sku, 1)
            usable, is_blocked = _claim_stock(sku, needed, working)
            if is_blocked:
                order_is_blocked = True
                entry = blocked_by_sku.setdefault(sku, {"needed_total": 0.0})
                entry["needed_total"] += max(0.0, needed - usable)

        if is_overdue and order_is_blocked:
            shortage_overdue_count += 1

    # Held/Compliance count — on_hold orders, distinct by Order ID,
    # from the FULL (unscoped) row set, same definition as the
    # dashboard's own Held/Compliance panel.
    held_order_ids = {
        r["Order ID"] for r in rows if r.get("Shipment status") == "on_hold"
    }
    held_count = len(held_order_ids)

    # Off-hours order pile-up, logged at snapshot time so this builds
    # real history going forward (can't be reconstructed retroactively
    # for days before this was added, since live_queue is fully replaced
    # every pull and doesn't keep an order-by-order history of its own).
    now = datetime.now(timezone.utc)
    off_hours_window = current_off_hours_window(now)
    off_hours_count = count_off_hours_orders(rows, off_hours_window)
    off_hours_gap_type = off_hours_window["gap_type"]

    # Kept as a secondary, still-useful figure (total build burden in
    # units) — no longer what drives the Build Health card's status,
    # which uses shortage_overdue_count/queue_count instead.
    build_units = 0.0
    for sku, entry in blocked_by_sku.items():
        s = stock.get(sku, {})
        reorder_max = s.get("reorder_point_max")
        usable = s.get("usable_stock", 0.0) or 0.0
        backorder = s.get("units_backorder", 0.0) or 0.0
        if reorder_max is not None:
            build_qty = max(0.0, reorder_max - usable)
        else:
            build_qty = max(backorder, entry["needed_total"])
        build_units += build_qty

    build_units_pct = (build_units / total_units * 100) if total_units else 0.0
    build_hit_rate_pct = (100 - (shortage_overdue_count / queue_count * 100)) if queue_count else 100.0

    return {
        "queue_count": queue_count,
        "warehouse_count": warehouse_count,
        "freight_count": freight_count,
        "both_count": both_count,
        "held_count": held_count,
        "off_hours_count": off_hours_count,
        "off_hours_gap_type": off_hours_gap_type,
        "bucket_0_24h": bucket_0_24h,
        "bucket_24_48h": bucket_24_48h,
        "bucket_48_72h": bucket_48_72h,
        "bucket_72h_plus": bucket_72h_plus,
        "total_units": total_units,
        "build_units": round(build_units),
        "build_pct": round(build_units_pct, 1),
        "shortage_overdue_count": shortage_overdue_count,
        "build_hit_rate_pct": round(build_hit_rate_pct, 1),
        "aged_24h_count": aged_24h,
        "aged_48h_count": aged_48h,
    }


def log_health_snapshot(rows: list[dict], stock: dict[str, dict], pulled_at: str) -> None:
    """Appends one row to health_snapshots — unlike live_queue/
    stock_levels, this is a history log, not a "right now" replace."""
    snapshot = compute_health_snapshot(rows, stock)
    snapshot["pulled_at"] = pulled_at
    client = get_supabase_client()
    client.table("health_snapshots").insert(snapshot).execute()
    print(f"Logged health snapshot: {snapshot}")


def log_turnover_snapshot(pulled_at: str) -> None:
    """Writes one row per product per day to turnover_snapshots — a
    daily history log (re-running the same day replaces that day) (same spirit as health_snapshots),
    not a "right now" replace like stock_levels. This is what inventory
    turnover is computed from: (units or dollars sold over a period) /
    (average of units_on_hand / total_value across that period's daily
    snapshots). Daily cadence confirmed sufficient — inventory levels
    don't move fast enough to need the 30-min cadence health_snapshots
    uses. fba_* columns tracked separately per Casey's call to report
    Amazon FBA stock separately rather than merge it into or drop it
    from the main figures, since it isn't held at Lab Alley's own
    physical location."""
    valuation = fetch_inventory_valuation()
    client = get_supabase_client()
    snapshot_rows = [
        {
            "pulled_at": pulled_at,
            "product_id": pid,
            "units_on_hand": v["units_on_hand"],
            "total_value": v["total_value"],
            "fba_units_on_hand": v["fba_units_on_hand"],
            "fba_total_value": v["fba_total_value"],
        }
        for pid, v in valuation.items()
    ]
    # ONE row per product per day. CONFIRMED BUG, FIXED (2026-10-06): this
    # used to be a blind append, so running the pull twice on the same day
    # (a manual run plus the daily trigger -- 2026-10-02 was logged more
    # than once) stacked a second full copy of that day's rows, and the
    # average-inventory side of the turnover math counts rows, so doubled
    # days were quietly weighted double. Now a re-run REPLACES that day's
    # rows. (A unique index in the database enforces the same rule.)
    client.table("turnover_snapshots").delete().eq("pulled_at", pulled_at).execute()
    # Reuses the same batching helper already proven for stock_levels'
    # bulk writes, rather than a new one-off chunking loop.
    insert_in_batches(client, "turnover_snapshots", snapshot_rows)
    print(f"Logged turnover snapshot for {len(snapshot_rows)} products at {pulled_at}")


def read_stock_levels() -> dict[str, dict]:
    """Reads the current stock_levels table back from Supabase (written
    by pull_stock_levels.py, roughly the same 30-min cadence as the live
    queue pull) and returns it as {product_id: {usable_stock,
    reorder_point_max, units_backorder}} — the shape
    compute_health_snapshot() needs. Deliberately reads the
    already-written table rather than re-fetching from Finale directly:
    avoids a second, redundant hit against the same slow/heavy Finale
    reports pull_stock_levels.py already pulls on its own schedule."""
    client = get_supabase_client()
    result = client.table("stock_levels").select("*").execute()
    stock: dict[str, dict] = {}
    for row in result.data or []:
        pid = row.get("product_id")
        if not pid:
            continue
        stock[pid] = {
            "usable_stock": row.get("usable_stock") or 0.0,
            "reorder_point_max": row.get("reorder_point_max"),
            "units_backorder": row.get("units_backorder") or 0.0,
        }
    return stock
