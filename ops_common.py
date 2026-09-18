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
    "lots": {lot_id: qty}}}, quantity summed only across sublocations
    that pass is_usable_sublocation() — excludes Quality Hold, Inventory
    Hold, Do Not Inventory, Amazon FBA stock, and staging/processing
    areas that aren't real, currently-pickable inventory. This is what
    makes "38.8 units on hand" correctly read as "36.3 usable" when
    ~2.5 of those units are actually sitting in Quality Hold — the exact
    real example that started this feature.

    The per-lot breakdown (confirmed against real data, 2026-09-18) is
    what powers the same-lot fulfillment check: a single lot's stock is
    often split across several usable sublocations (e.g. one real lot,
    '5001997/21.1', appeared at 4 different bins for the same product),
    so this sums by lot ID across all of them, not just one row each."""
    rows = fetch_finale_report(STOCK_BY_SUBLOCATION_REPORT_URL)
    stock: dict[str, dict] = {}
    for row in rows:
        sublocation = row.get("Sublocation")
        pid = row.get("Product ID")
        if not sublocation or not pid:
            continue
        pid = pid.strip()
        if pid not in stock:
            stock[pid] = {"usable_stock": 0.0, "description": row.get("Description") or "", "lots": {}}
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
    wrong, just Finale occasionally timing out on a slow, heavy report).
    Does NOT retry on actual HTTP error responses (4xx/5xx with a real
    response) — those indicate a genuine problem worth seeing immediately
    rather than masking with a retry.
    """
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, auth=(FINALE_API_KEY, FINALE_API_SECRET), timeout=180)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == max_attempts:
                raise
            wait = 10 * attempt  # 10s, then 20s
            print(f"  Finale request failed ({e.__class__.__name__}), "
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


def parse_chicago_naive(naive_str: str):
    """Parses a naive datetime string (no timezone marker) as wall-clock
    time in America/Chicago, returning a timezone-aware UTC datetime.
    Python's zoneinfo resolves CST/CDT correctly natively here — unlike
    the frontend's JS equivalent (parseChicagoNaive in site/index.html),
    which needs an iterative correction trick to get the same result,
    since JS has no first-class named-timezone parsing. Verified against
    the exact same test cases (including both DST transition edges) as
    that JS version to confirm they agree."""
    if not naive_str:
        return None
    try:
        base = naive_str.split(".")[0]
        naive_dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    chicago_dt = naive_dt.replace(tzinfo=CHICAGO_TZ)
    return chicago_dt.astimezone(timezone.utc)


def business_hours_elapsed(start: datetime, end: datetime) -> float:
    """Hours elapsed between start and end, weekends fully excluded
    (Saturday 00:00 through Monday 00:00, Chicago time) — the 48-hour
    SLA clock pauses at midnight Friday night, resumes midnight Monday.
    Mirrors the frontend's businessHoursElapsed(); verified against the
    same 4 test scenarios (including a span landing entirely inside one
    weekend) to confirm the two implementations agree."""
    if not start or end <= start:
        return 0.0
    total_seconds = (end - start).total_seconds()
    excluded_seconds = 0.0
    cursor = start.astimezone(CHICAGO_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor.astimezone(timezone.utc) < end:
        day_end = cursor + timedelta(days=1)
        if cursor.weekday() in (5, 6):  # Saturday=5, Sunday=6
            day_start_utc = cursor.astimezone(timezone.utc)
            day_end_utc = day_end.astimezone(timezone.utc)
            overlap_start = max(day_start_utc, start)
            overlap_end = min(day_end_utc, end)
            if overlap_end > overlap_start:
                excluded_seconds += (overlap_end - overlap_start).total_seconds()
        cursor = day_end
    return (total_seconds - excluded_seconds) / 3600.0


def business_hours_since(naive_str: str) -> float:
    start = parse_chicago_naive(naive_str)
    if not start:
        return 0.0
    return business_hours_elapsed(start, datetime.now(timezone.utc))


def compute_health_snapshot(rows: list[dict], stock: dict[str, dict]) -> dict:
    """Server-side port of the frontend's buildShippingList() /
    buildProductionList() logic (site/index.html) — specifically the
    parts needed to compute the Build Health indicator's "total build
    units" figure for a periodic history snapshot. The Supabase
    publishable key is deliberately read-only, so the frontend can never
    write these snapshots itself; this is why the computation has to be
    duplicated here rather than shared.

    IMPORTANT: this MUST stay logically consistent with the JS version.
    If the frontend's shortage/pickability logic changes, this needs a
    matching update, or Build Health's live number and its own logged
    history will silently drift apart. There is deliberately no shared
    source of truth between them (JS runs in the browser, this runs in
    GitHub Actions) — this comment is the closest thing to one.

    `rows` is the same shape build_rows() produces (Title Case keys,
    pre-to_db_rows). `stock` is {product_id: {usable_stock,
    reorder_point_max, units_backorder}} as returned by
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

    queue_count = len(by_order)
    total_units = 0
    aged_24h = 0
    aged_48h = 0
    blocked_by_sku: dict[str, dict] = {}

    for oid, lines in by_order.items():
        first = lines[0]
        total_units += int(first.get("Item Quantity") or 0)

        hours = business_hours_since(first.get("Order datetime"))
        if hours >= 48:
            aged_48h += 1
        elif hours >= 24:
            aged_24h += 1

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

        for sku in distinct_skus:
            needed = qty_by_sku.get(sku, 1)
            s = stock.get(sku, {})
            usable = s.get("usable_stock", 0.0) or 0.0
            if usable < needed:
                entry = blocked_by_sku.setdefault(sku, {"needed_total": 0.0})
                entry["needed_total"] += max(0.0, needed - usable)

def _is_case_sku(sku: str) -> bool:
    """All Lab Alley case SKUs end in 'CS', nothing ever follows it —
    confirmed directly. Mirrors isCaseSku() in site/index.html."""
    return sku.endswith("CS")


def _pair_sku_for(sku: str) -> str:
    """Case -> single: strip trailing 'CS'. Single -> case: append 'CS'.
    1 case = 4 singles, universally. Mirrors pairSkuFor()."""
    return sku[:-2] if _is_case_sku(sku) else sku + "CS"


def _resolve_sku_availability(sku: str, stock: dict[str, dict]) -> float:
    """Effective availability accounting for case<->single conversion —
    mirrors resolveSkuAvailability() in site/index.html exactly."""
    direct = stock.get(sku, {})
    direct_usable = direct.get("usable_stock", 0.0) or 0.0
    pair = stock.get(_pair_sku_for(sku), {})
    pair_usable = pair.get("usable_stock", 0.0) or 0.0
    if _is_case_sku(sku):
        return direct_usable + (pair_usable // 4)
    return direct_usable + (pair_usable * 4)


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
    CONFIRMED DRIFT ALREADY HAPPENED ONCE (2026-09-18): the Build %
    formula was redefined on the frontend (from "total build units vs
    queue units" to "orders 48h+ overdue AND shortage-blocked vs total
    queue orders") and this function wasn't updated to match at the same
    time — the first real logged snapshot used the old formula. Fixed
    here; if this happens again, that's the bug to look for.

    `rows` is the same shape build_rows() produces (Title Case keys,
    pre-to_db_rows). `stock` is {product_id: {usable_stock,
    reorder_point_max, units_backorder}} as returned by
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

    queue_count = len(by_order)
    total_units = 0
    aged_24h = 0
    aged_48h = 0
    shortage_overdue_count = 0  # 48h+ overdue AND currently shortage-blocked
    blocked_by_sku: dict[str, dict] = {}

    for oid, lines in by_order.items():
        first = lines[0]
        total_units += int(first.get("Item Quantity") or 0)

        hours = business_hours_since(first.get("Order datetime"))
        is_overdue = hours >= 48
        if is_overdue:
            aged_48h += 1
        elif hours >= 24:
            aged_24h += 1

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
            usable = _resolve_sku_availability(sku, stock)
            if usable < needed:
                order_is_blocked = True
                entry = blocked_by_sku.setdefault(sku, {"needed_total": 0.0})
                entry["needed_total"] += max(0.0, needed - usable)

        if is_overdue and order_is_blocked:
            shortage_overdue_count += 1

    # Kept as a secondary, still-useful figure (total build burden in
    # units) — no longer what drives the Build Health card's status,
    # which now uses shortage_overdue_count/queue_count instead (see
    # below).
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
