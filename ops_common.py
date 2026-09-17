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
import time
import json
import requests
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
    """JSON-encoded list of {sku, name} for every real line item
    ShipStation has on this order — captured because Finale's own
    Orders report sometimes collapses a split/backordered order's
    distinct products into a single row literally labeled "Multiple
    products" for both Product ID and Description, giving us no way to
    tell which real SKUs were actually involved. ShipStation's order
    data always has the genuine per-item detail regardless of how
    Finale chose to summarize it, so this is the fallback the frontend
    uses specifically for those collapsed rows."""
    items = order.get("items") or []
    return json.dumps([
        {"sku": (item.get("sku") or "").strip(), "name": (item.get("name") or "").strip()}
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
