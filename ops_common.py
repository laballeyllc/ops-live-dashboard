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

# The two tags that define "our" core Warehouse/Freight queue. Everything
# else (Biologix, United Scientific, Transene, GT, Post, holds, etc.) is
# a real tag too, just not part of this specific queue.
CORE_QUEUE_TAGS = {"Austin Warehouse": "Warehouse", "ATX Freight": "Freight"}

# "Jerry" was an early, wrong guess at the Freight tag name — turned out
# to be the name of a saved filter/view in ShipStation, not an actual
# tag. Left empty since the raw tag names are now verified and trusted;
# add overrides here only once confirmed against a real order.
TAG_DISPLAY_OVERRIDES: dict[str, str] = {}


def normalize_order_id(value) -> str:
    """Order IDs can come back as int or str depending on the source;
    normalize to a plain string for matching."""
    if value is None:
        return ""
    return str(value).strip()


def tags_for_order(order: dict, tag_name_by_id: dict[int, str]) -> str:
    """Comma-separated real tag name(s) for an order, applying any display
    override and trimming stray whitespace (ShipStation has at least one
    tag name with a trailing space — "ATX Freight "). An order can carry
    more than one tag (e.g. a split order needing both Warehouse and a
    drop-ship vendor), so this is deliberately not a single value."""
    tag_ids = order.get("tagIds") or []
    names = [tag_name_by_id.get(tid, f"(unknown tag {tid})").strip() for tid in tag_ids]
    names = [TAG_DISPLAY_OVERRIDES.get(n, n) for n in names]
    return ", ".join(names)


def core_queue(tags: str) -> str:
    """Simplified, filterable queue label derived from the raw Tags
    field: 'Warehouse', 'Freight', 'Both' (a split order carrying both
    tags), or '' (everything else — other vendor queues, holds, no tag)."""
    present = [label for tag, label in CORE_QUEUE_TAGS.items() if tag in tags]
    if len(present) == 2:
        return "Both"
    if present:
        return present[0]
    return ""


def fetch_finale_report(url: str) -> list[dict]:
    """Runs a saved Finale report fresh via the Reporting API. This is the
    single most expensive step in either script (the Orders report has
    150k+ rows) — Finale's Reporting API has no way to ask for "just
    these N order IDs," so both the full historical pull and the
    live-queue pull have to download the whole thing and filter locally."""
    resp = requests.get(url, auth=(FINALE_API_KEY, FINALE_API_SECRET), timeout=180)
    resp.raise_for_status()
    return resp.json()


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
    "Product ID": "product_id",
    "Description": "description",
    "Category": "category",
    "CHEM": "chem",
    "CHEM TECH": "chem_tech",
    "LAB ROOM": "lab_room",
    "Downpack Product": "downpack",
    "HAZMAT": "hazmat",
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
