"""
KPI Summary sync: pulls ShipStation, Finale, and Google Sheets data into
the kpi_* tables in Supabase, so site/kpi.html can total any date range
instantly through kpi_summary().

Why a sync and not live calls from the page: Finale's reports are too
large to generate inside a Netlify function's time limit (the Orders
report alone is 150k+ rows), and long ranges ("Last year", "All time")
would mean thousands of paged ShipStation calls per click.

Each section runs independently. A failure in one is logged to
kpi_sync_log and printed, and the others still run.

Usage:
    python pull_kpi_data.py                         # last 3 days through today (Central)
    python pull_kpi_data.py --nightly               # also refresh product attributes
    python pull_kpi_data.py --start 2025-01-01 --end 2026-10-05   # backfill ShipStation + sales
    python pull_kpi_data.py --only shipstation,sheets              # run selected sections

Sections: shipstation, queue, attrs, sales, builds, pos, receipts, sheets

Environment (GitHub Actions secrets / variables):
    SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET
    FINALE_API_KEY, FINALE_API_SECRET
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    GOOGLE_SERVICE_ACCOUNT_JSON          (whole JSON key file contents)
    FINALE_BUILDS_REPORT_URL             (Finale report JSON URL, optional)
    FINALE_PO_REPORT_URL                 (optional)
    FINALE_RECEIPTS_REPORT_URL           (optional)
"""
import os
import re
import sys
import json
import hashlib
import argparse
import traceback
from datetime import datetime, date, timedelta, timezone

import requests
from dotenv import load_dotenv

from shipstation_client import ShipStationClient
from ops_common import (
    CHICAGO_TZ,
    WAREHOUSE_LOCATION_NAME, FREIGHT_LOCATION_NAME,
    ORDERS_REPORT_URL,
    parse_shipstation_naive, business_hours_elapsed,
    fetch_finale_report, fetch_product_sales_totals,
    get_supabase_client,
)

load_dotenv()

BATCH = 500
END_OF_SHIFT_HOUR = 16  # 4 PM Central: assumed ship time when no label timestamp exists

WEEKLY_OPS_SHEET_ID = "15rlaJJB6Ag3syCU9Ro0dwbEzf1vGKRcS-qBanvPYvIc"   # "Data" tab: cycle counts
NEW_PRODUCT_SHEET_ID = "1UBikYa3_1Xg1v6qIDjZci4C_BESksB0saEt4d8pThoE"  # "Product Staging" tab

# First cycle-count location column on the Data tab. Every column from
# here to the end of the header row is treated as a location.
FIRST_LOCATION_HEADER = "ROW 1"


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def central_today() -> date:
    return datetime.now(CHICAGO_TZ).date()


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def upsert(client, table: str, rows: list[dict], on_conflict: str) -> None:
    for i in range(0, len(rows), BATCH):
        client.table(table).upsert(rows[i:i + BATCH], on_conflict=on_conflict).execute()
    print(f"  upserted {len(rows)} rows into {table}")


def log(client, section: str, ok: bool, detail: str) -> None:
    try:
        client.table("kpi_sync_log").insert({"section": section, "ok": ok, "detail": detail[:2000]}).execute()
    except Exception as e:  # logging must never take the run down
        print(f"  (could not write kpi_sync_log: {e})")


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y")


def parse_any_date(value):
    """Dates arrive in several shapes across Finale and Sheets. Returns a
    date or None; never guesses on something it can't parse."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:          # epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        if 20000 < value < 80000:           # Sheets serial date
            return date(1899, 12, 30) + timedelta(days=int(value))
        return None
    s = str(value).strip()
    if not s or s == "--":
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        s = s[:10]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(row: dict, candidates: list[str]):
    """First non-empty value among candidate column names (exact match,
    then case/whitespace-insensitive)."""
    for c in candidates:
        v = row.get(c)
        if v not in (None, "", "--"):
            return v
    norm = {re.sub(r"\s+", " ", k).strip().lower(): v for k, v in row.items() if isinstance(k, str)}
    for c in candidates:
        v = norm.get(c.lower())
        if v not in (None, "", "--"):
            return v
    return None


# ---------------------------------------------------------------------
# ShipStation
# ---------------------------------------------------------------------

def ss_paged(client: ShipStationClient, path: str, params: dict, key: str) -> list[dict]:
    out, page = [], 1
    while True:
        data = client._get(path, {**params, "page": page, "pageSize": 500})
        out.extend(data.get(key) or [])
        if page >= (data.get("pages") or 1):
            return out
        page += 1


WAREHOUSE_TAG = "austin warehouse"
FREIGHT_TAG = "atx freight"


def queue_name(order_or_shipment: dict, warehouse_id, freight_id, tag_names: dict | None = None) -> str:
    """Ship From Location when it's set to one of the two Dripping Springs
    locations (how Ops Live classifies, reliable since mid-2026). Before
    that, orders were 'Unassigned', so fall back to the tags that were in
    use throughout: 'Austin Warehouse' and 'ATX Freight'. An order with
    both tags (a split order) counts as Freight."""
    wh = (order_or_shipment.get("advancedOptions") or {}).get("warehouseId", order_or_shipment.get("warehouseId"))
    if wh == warehouse_id:
        return "Warehouse"
    if wh == freight_id:
        return "Freight"
    if tag_names is not None:
        tags = {str(tag_names.get(t, "")).strip().lower() for t in (order_or_shipment.get("tagIds") or [])}
        if FREIGHT_TAG in tags:
            return "Freight"
        if WAREHOUSE_TAG in tags:
            return "Warehouse"
    return ""


def items_of(order: dict) -> list[dict]:
    items = []
    for it in order.get("items") or []:
        sku = (it.get("sku") or "").strip()
        qty = int(it.get("quantity") or 0)
        if sku and qty:
            items.append({"sku": sku, "qty": qty})
    return items


def end_of_shift_instant(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, END_OF_SHIFT_HOUR, tzinfo=CHICAGO_TZ).astimezone(timezone.utc)


def order_row(o: dict, wh_id, fr_id, tag_names: dict) -> dict:
    """Saves what ShipStation has on the order itself. Label-based ship
    dates and times are filled in afterwards by reconcile_ship_times()."""
    oid = o["orderId"]
    order_dt = parse_shipstation_naive(o.get("orderDate"))
    ship_date = parse_any_date((o.get("shipDate") or "")[:10]) if o.get("orderStatus") == "shipped" else None
    ship_dt, source = None, None
    if ship_date:
        ship_dt, source = end_of_shift_instant(ship_date), "end_of_shift"
    items = items_of(o)
    return {
        "order_id": oid,
        "order_number": o.get("orderNumber"),
        "queue": queue_name(o, wh_id, fr_id, tag_names),
        "status": o.get("orderStatus"),
        "order_dt": order_dt.isoformat() if order_dt else None,
        "order_date": order_dt.astimezone(CHICAGO_TZ).date().isoformat() if order_dt else None,
        "ship_date": ship_date.isoformat() if ship_date else None,
        "ship_dt": ship_dt.isoformat() if ship_dt else None,
        "ship_dt_source": source,
        "queue_bhours": round(business_hours_elapsed(order_dt, ship_dt), 2) if (order_dt and ship_dt) else None,
        "units": sum(i["qty"] for i in items),
        "items": items,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def sync_shipstation_window(ss, sb, start: date, end: date, wh_id, fr_id, tag_names: dict) -> None:
    s_str, e_str = f"{start} 00:00:00", f"{end} 23:59:59"
    print(f"ShipStation {start} to {end}")

    placed = ss_paged(ss, "/orders", {"orderDateStart": s_str, "orderDateEnd": e_str}, "orders")
    print(f"  {len(placed)} orders placed")
    # Orders shipped in the window but placed before it. ShipStation has no
    # shipDate filter, so this uses a modifyDate net around the window --
    # reliable for recent dates (hourly runs); finds nothing for old dates.
    shipped = ss_paged(ss, "/orders", {
        "orderStatus": "shipped",
        "modifyDateStart": f"{start - timedelta(days=3)} 00:00:00",
        "modifyDateEnd": f"{end + timedelta(days=3)} 23:59:59",
    }, "orders")
    shipped = [o for o in shipped if start.isoformat() <= (o.get("shipDate") or "")[:10] <= end.isoformat()]
    print(f"  {len(shipped)} older orders shipped (found by modify date)")
    shipments = ss_paged(ss, "/shipments", {
        "shipDateStart": start.isoformat(), "shipDateEnd": end.isoformat(),
        "includeShipmentItems": "false",
    }, "shipments")
    print(f"  {len(shipments)} shipment records")

    shipment_rows = []
    for sh in shipments:
        created = parse_shipstation_naive(sh.get("createDate"))
        shipment_rows.append({
            "shipment_id": sh.get("shipmentId"),
            "order_id": sh.get("orderId"),
            "order_number": str(sh.get("orderNumber") or "").strip() or None,
            "queue": queue_name(sh, wh_id, fr_id),
            "ship_date": (sh.get("shipDate") or "")[:10] or None,
            "create_dt": created.isoformat() if created else None,
            "voided": bool(sh.get("voided")),
        })

    window_orders: dict[int, dict] = {}
    for o in placed + shipped:          # shipped second: its status wins on overlap
        window_orders[o["orderId"]] = o
    rows = [order_row(o, wh_id, fr_id, tag_names) for o in window_orders.values()]
    upsert(sb, "kpi_orders", rows, "order_id")
    upsert(sb, "kpi_shipments", [r for r in shipment_rows if r["shipment_id"]], "shipment_id")


def fetch_all(sb, table: str, columns: str, apply_filters) -> list[dict]:
    out, page = [], 0
    while True:
        q = apply_filters(sb.table(table).select(columns)).order(columns.split(",")[0]).range(page * 1000, page * 1000 + 999)
        data = q.execute().data or []
        out.extend(data)
        if len(data) < 1000:
            return out
        page += 1


def reconcile_ship_times(sb, start: date, end: date) -> str:
    """Matches labels to orders by ORDER NUMBER (2025 labels point to
    order IDs that no longer exist; the orders were re-imported with new
    IDs, but order numbers carried over). For each shipped order:
      - ship date: the order's own shipDate, else its earliest label's
      - ship time: earliest label's creation time, else 4 PM that day
    Also gives each label its order's Warehouse/Freight queue, since
    pre-2026 labels came from a ship-from location that no longer exists."""
    since = (start - timedelta(days=45)).isoformat()
    labels = fetch_all(sb, "kpi_shipments", "shipment_id,order_number,ship_date,create_dt,voided,queue",
                       lambda q: q.gte("ship_date", since).eq("voided", False))
    first_label: dict[str, dict] = {}
    for l in labels:
        num = l.get("order_number")
        if not num or not l.get("ship_date"):
            continue
        key = (l.get("create_dt") or l["ship_date"])
        if num not in first_label or key < (first_label[num].get("create_dt") or first_label[num]["ship_date"]):
            first_label[num] = l

    orders = fetch_all(sb, "kpi_orders", "order_id,order_number,status,order_dt,ship_date,ship_dt_source,queue",
                       lambda q: q.gte("order_date", since).lte("order_date", end.isoformat()))
    queue_by_number = {}
    updates = []
    for o in orders:
        num = str(o.get("order_number") or "").strip()
        if num and o.get("queue"):
            queue_by_number[num] = o["queue"]
        if o.get("status") != "shipped":
            continue
        lab = first_label.get(num)
        if not lab:
            continue
        ship_date = o.get("ship_date") or lab["ship_date"]
        if lab.get("create_dt"):
            ship_dt, source = datetime.fromisoformat(lab["create_dt"]), "label"
        else:
            ship_dt, source = end_of_shift_instant(parse_any_date(ship_date)), "end_of_shift"
        if o.get("ship_dt_source") == source and o.get("ship_date") == ship_date and source == "label":
            continue
        order_dt = datetime.fromisoformat(o["order_dt"]) if o.get("order_dt") else None
        updates.append({
            "order_id": o["order_id"],
            "ship_date": ship_date,
            "ship_dt": ship_dt.isoformat(),
            "ship_dt_source": source,
            "queue_bhours": round(business_hours_elapsed(order_dt, ship_dt), 2) if order_dt else None,
        })
    if updates:
        upsert(sb, "kpi_orders", updates, "order_id")

    label_queue = [{"shipment_id": l["shipment_id"], "queue": queue_by_number[l["order_number"]]}
                   for l in labels
                   if not l.get("queue") and l.get("order_number") in queue_by_number]
    if label_queue:
        upsert(sb, "kpi_shipments", label_queue, "shipment_id")
    return f"ship dates/times from labels on {len(updates)} orders; queue set on {len(label_queue)} labels"


def section_shipstation(sb, start: date, end: date) -> str:
    ss = ShipStationClient()
    wh_id = ss.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    fr_id = ss.get_warehouse_id(FREIGHT_LOCATION_NAME)
    tag_names = ss.list_tags()
    chunk_start = start
    while chunk_start <= end:      # weekly chunks keep each request set small on backfills
        chunk_end = min(chunk_start + timedelta(days=6), end)
        sync_shipstation_window(ss, sb, chunk_start, chunk_end, wh_id, fr_id, tag_names)
        chunk_start = chunk_end + timedelta(days=1)
    detail = reconcile_ship_times(sb, start, end)
    print(f"  {detail}")
    return f"{start} to {end}: {detail}"


def section_queue(sb) -> str:
    """Today's Warehouse/Freight queue, units per product. Overwrites
    today's row on each run, so each day keeps its last snapshot."""
    ss = ShipStationClient()
    wh_id = ss.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    fr_id = ss.get_warehouse_id(FREIGHT_LOCATION_NAME)
    tag_names = ss.list_tags()
    queued = ss_paged(ss, "/orders", {"orderStatus": "awaiting_shipment"}, "orders")
    units: dict[str, float] = {}
    for o in queued:
        if queue_name(o, wh_id, fr_id, tag_names) not in ("Warehouse", "Freight"):
            continue
        for it in items_of(o):
            units[it["sku"]] = units.get(it["sku"], 0) + it["qty"]
    today = central_today().isoformat()
    sb.table("kpi_queue_daily").delete().eq("snap_date", today).execute()
    rows = [{"snap_date": today, "product_id": k, "units": v} for k, v in units.items()]
    if rows:
        upsert(sb, "kpi_queue_daily", rows, "snap_date,product_id")
    return f"{len(rows)} products, {sum(units.values()):.0f} units in queue"


# ---------------------------------------------------------------------
# Finale
# ---------------------------------------------------------------------

def section_attrs(sb) -> str:
    """Product attributes from the same Finale Orders report Ops Live uses
    (Category, CHEM, LAB ROOM, Downpack Product, HAZMAT)."""
    rows = fetch_finale_report(ORDERS_REPORT_URL)
    attrs: dict[str, dict] = {}
    for r in rows:
        pid = (r.get("Product ID") or "").strip()
        if not pid or pid == "Multiple products":
            continue
        entry = attrs.setdefault(pid, {"product_id": pid})
        for col, key in (("Category", "category"), ("CHEM", "chem"), ("LAB ROOM", "lab_room"),
                         ("Downpack Product", "downpack"), ("HAZMAT", "hazmat")):
            v = r.get(col)
            if v not in (None, "", "--"):
                entry[key] = str(v).strip()
    now = datetime.now(timezone.utc).isoformat()
    out = [{"category": None, "chem": None, "lab_room": None, "downpack": None, "hazmat": None,
            **a, "updated_at": now} for a in attrs.values()]
    upsert(sb, "kpi_product_attrs", out, "product_id")
    return f"{len(out)} products"


def section_sales(sb, start: date, end: date) -> str:
    """Daily sales totals for turnover. Only for days that have an
    inventory snapshot, since turnover can't be computed without one."""
    first = sb.table("kpi_inventory_daily").select("snap_date").order("snap_date").limit(1).execute().data
    if not first:
        return "skipped: no inventory snapshots yet"
    first_day = parse_any_date(first[0]["snap_date"])
    days = [d for d in daterange(max(start, first_day), min(end, central_today()))]
    rows = []
    for d in days:
        totals = fetch_product_sales_totals(d.isoformat(), d.isoformat())
        rows.append({
            "sale_date": d.isoformat(),
            "units": sum(t["units_sold"] for t in totals.values()),
            "dollars": sum(t["dollars_sold"] for t in totals.values()),
        })
    if rows:
        upsert(sb, "kpi_sales_daily", rows, "sale_date")
    return f"{len(rows)} days"


def fill_down(rows: list[dict], keys: list[str]) -> list[dict]:
    """Finale pivot reports put a group value (Product ID, Supplier...) on
    a header row and leave it blank on the detail rows below. Carry the
    last seen value down so every detail row is self-contained."""
    carried: dict[str, object] = {}
    out = []
    for r in rows:
        r = dict(r)
        for k in keys:
            if r.get(k) not in (None, "", "--"):
                carried[k] = r[k]
            elif k in carried:
                r[k] = carried[k]
        out.append(r)
    return out


def report_rows(env_name: str, fill_keys: list[str]) -> list[dict] | None:
    url = os.environ.get(env_name, "").strip()
    if not url:
        return None
    return fill_down(fetch_finale_report(url), fill_keys)


def section_builds(sb) -> str:
    rows = report_rows("FINALE_BUILDS_REPORT_URL", ["Product ID", "Product"])
    if rows is None:
        return "skipped: FINALE_BUILDS_REPORT_URL not set"
    out, seen = [], set()
    for r in rows:
        bid = pick(r, ["Build ID", "Build", "Build id"])
        done = parse_any_date(pick(r, ["Complete date actual", "Complete date", "Completed date"]))
        qty = to_number(pick(r, ["Quantity to produce", "Quantity", "Quantity produced", "Units"]))
        pid = pick(r, ["Product ID", "Product"])
        if not bid or str(bid) == "TOTAL:" or not done or not qty:
            continue
        key = str(bid)
        if key in seen:
            continue
        seen.add(key)
        out.append({"build_id": key, "product_id": str(pid or "").strip(), "quantity": qty,
                    "complete_date": done.isoformat()})
    if not out:
        raise RuntimeError(f"Builds report returned {len(rows)} rows but none parsed. "
                           f"Columns seen: {sorted(rows[0].keys()) if rows else 'none'}")
    upsert(sb, "kpi_builds", out, "build_id")
    return f"{len(out)} builds"


def section_pos(sb) -> str:
    rows = report_rows("FINALE_PO_REPORT_URL", ["Supplier"])
    if rows is None:
        return "skipped: FINALE_PO_REPORT_URL not set"
    out: dict[str, dict] = {}
    for r in rows:
        po = pick(r, ["Order ID", "PO ID", "Purchase order ID", "Order"])
        if not po or str(po) == "TOTAL:":
            continue
        d = parse_any_date(pick(r, ["Order date", "Order Date", "Date"]))
        if not d:
            continue
        val = to_number(pick(r, ["Subtotal sum", "Subtotal", "Total", "Order total"])) or 0.0
        key = str(po).strip()
        entry = out.setdefault(key, {"po_id": key, "supplier": str(pick(r, ["Supplier"]) or "").strip(),
                                     "order_date": d.isoformat(), "value": 0.0})
        entry["value"] += val   # one row per PO, or one per line: either way this sums correctly
    if not out:
        raise RuntimeError(f"PO report returned {len(rows)} rows but none parsed. "
                           f"Columns seen: {sorted(rows[0].keys()) if rows else 'none'}")
    upsert(sb, "kpi_purchase_orders", list(out.values()), "po_id")
    return f"{len(out)} purchase orders"


def section_receipts(sb) -> str:
    rows = report_rows("FINALE_RECEIPTS_REPORT_URL", ["Product ID", "Product", "Supplier"])
    if rows is None:
        return "skipped: FINALE_RECEIPTS_REPORT_URL not set"
    out: dict[str, dict] = {}
    for r in rows:
        ordered = parse_any_date(pick(r, ["Order date", "Order Date", "PO order date"]))
        received = parse_any_date(pick(r, ["Receive date", "Received date", "Receive date actual",
                                           "Shipment receive date", "Date received"]))
        if not ordered or not received or received < ordered:
            continue
        pid = str(pick(r, ["Product ID", "Product"]) or "").strip()
        supplier = str(pick(r, ["Supplier"]) or "").strip()
        ref = str(pick(r, ["Shipment ID", "Receipt ID", "Order ID"]) or "")
        key = hashlib.md5(f"{ref}|{pid}|{supplier}|{ordered}|{received}".encode()).hexdigest()
        out[key] = {"receipt_key": key, "product_id": pid, "supplier": supplier,
                    "order_date": ordered.isoformat(), "receive_date": received.isoformat(),
                    "lead_days": (received - ordered).days}
    if not out:
        raise RuntimeError(f"Receipts report returned {len(rows)} rows but none parsed. "
                           f"Columns seen: {sorted(rows[0].keys()) if rows else 'none'}")
    upsert(sb, "kpi_receipts", list(out.values()), "receipt_key")
    return f"{len(out)} receipts"


# ---------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------

def sheets_token() -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(Request())
    return creds.token


def sheet_values(token: str, sheet_id: str, rng: str) -> list[list]:
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{requests.utils.quote(rng)}",
        params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code == 403:
        raise RuntimeError(f"No access to sheet {sheet_id}. Share it (Viewer) with the service account.")
    resp.raise_for_status()
    return resp.json().get("values", [])


def parse_accuracy(value):
    if isinstance(value, str) and value.strip().endswith("%"):
        n = to_number(value.strip()[:-1])
        return n / 100 if n is not None else None
    n = to_number(value)
    if n is None:
        return None
    return n / 100 if n > 1.5 else n


def parse_cycle_counts(values: list[list]) -> list[dict]:
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    if FIRST_LOCATION_HEADER not in header:
        raise RuntimeError(f"Data tab header has no '{FIRST_LOCATION_HEADER}' column.")
    first = header.index(FIRST_LOCATION_HEADER)
    locations = [(i, header[i]) for i in range(first, len(header)) if header[i]]
    out = []
    for row in values[1:]:
        week = parse_any_date(row[0]) if row else None
        if not week:
            continue
        for i, loc in locations:
            if i < len(row):
                acc = parse_accuracy(row[i])
                if acc is not None:
                    out.append({"count_week": week.isoformat(), "location": loc, "accuracy": acc})
    return out


WEEK_OF = re.compile(r"^\s*week of\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)


def parse_product_staging(values: list[list]) -> list[dict]:
    """'Week of M/D/YYYY' starts a block; each following row with a product
    name plus a grade or SKU is one product. Undated blocks are skipped."""
    out, week = [], None
    for idx, row in enumerate(values):
        cells = [str(c).strip() if c is not None else "" for c in row] + [""] * 4
        m = WEEK_OF.match(cells[0])
        if m:
            week = parse_any_date(m.group(1))
            continue
        if not week or cells[0].lower() in ("product", ""):
            continue
        product, grade, la_sku = cells[0], cells[1], cells[3]
        if not (grade or la_sku):
            continue
        key = hashlib.md5(f"{idx}|{week}|{product}|{grade}|{la_sku}".encode()).hexdigest()
        out.append({"row_key": key, "week_of": week.isoformat(), "product": product,
                    "grade": grade, "la_sku": la_sku})
    return out


def section_sheets(sb) -> str:
    token = sheets_token()
    counts = parse_cycle_counts(sheet_values(token, WEEKLY_OPS_SHEET_ID, "Data!A1:AZ3000"))
    if counts:
        upsert(sb, "kpi_cycle_counts", counts, "count_week,location")
    staged = parse_product_staging(sheet_values(token, NEW_PRODUCT_SHEET_ID, "Product Staging!A1:K2000"))
    sb.table("kpi_products_added").delete().neq("row_key", "").execute()
    if staged:
        upsert(sb, "kpi_products_added", staged, "row_key")
    return f"{len(counts)} cycle-count cells, {len(staged)} staged products"


# ---------------------------------------------------------------------
# Diagnostics: counts and IDs only, no customer details.
# ---------------------------------------------------------------------

def _top(counter: dict, n: int = 15) -> str:
    items = sorted(counter.items(), key=lambda kv: -kv[1])[:n]
    return "; ".join(f"{k}: {v}" for k, v in items) or "(none)"


def diagnose_window(ss, start: date, end: date, wh_names: dict, tag_names: dict, store_names: dict) -> None:
    print(f"\n######## {start} to {end} ########")
    placed = ss_paged(ss, "/orders", {"orderDateStart": f"{start} 00:00:00", "orderDateEnd": f"{end} 23:59:59"}, "orders")
    shipments = ss_paged(ss, "/shipments", {"shipDateStart": start.isoformat(), "shipDateEnd": end.isoformat(),
                                            "includeShipmentItems": "false"}, "shipments")
    print(f"orders placed: {len(placed)}   shipment records: {len(shipments)}")

    def count(fn, rows):
        c = {}
        for r in rows:
            k = fn(r)
            for kk in (k if isinstance(k, list) else [k]):
                c[kk] = c.get(kk, 0) + 1
        return c

    adv = lambda o: o.get("advancedOptions") or {}
    print("by status:        ", _top(count(lambda o: o.get("orderStatus"), placed)))
    print("by ship-from:     ", _top(count(lambda o: f"{adv(o).get('warehouseId')} ({wh_names.get(adv(o).get('warehouseId'), '?')})", placed)))
    print("by store:         ", _top(count(lambda o: store_names.get(adv(o).get("storeId"), adv(o).get("storeId")), placed)))
    print("by tag:           ", _top(count(lambda o: [tag_names.get(t, t) for t in (o.get("tagIds") or [])] or ["(no tags)"], placed), 20))
    shipped = [o for o in placed if o.get("orderStatus") == "shipped"]
    print(f"shipped orders with shipDate: {sum(1 for o in shipped if o.get('shipDate'))} of {len(shipped)}")
    print(f"externallyFulfilled: {sum(1 for o in placed if o.get('externallyFulfilled'))}   "
          f"mergedOrSplit: {sum(1 for o in placed if adv(o).get('mergedOrSplit'))}   "
          f"has parentId: {sum(1 for o in placed if adv(o).get('parentId'))}   "
          f"active=false: {sum(1 for o in placed if o.get('active') is False)}")
    print(f"order numbers containing '-': {sum(1 for o in placed if '-' in str(o.get('orderNumber') or ''))}")
    print("shipments by ship-from:", _top(count(lambda s: f"{s.get('warehouseId')} ({wh_names.get(s.get('warehouseId'), '?')})", shipments)))

    placed_ids = {o["orderId"] for o in placed}
    unmatched = [s for s in shipments if s.get("orderId") not in placed_ids and not s.get("voided")]
    print(f"shipments whose order was placed in this window: {len(shipments) - len(unmatched)}; not: {len(unmatched)}")
    for s in unmatched[:6]:
        print(f"  label {s.get('shipmentId')}: orderId {s.get('orderId')} orderNumber {s.get('orderNumber')} "
              f"shipDate {s.get('shipDate')} ship-from {s.get('warehouseId')}")
        try:
            o = ss._get(f"/orders/{s.get('orderId')}")
            a = o.get("advancedOptions") or {}
            print(f"    -> order {o.get('orderNumber')}: orderDate {o.get('orderDate')} createDate {o.get('createDate')} "
                  f"status {o.get('orderStatus')} shipDate {o.get('shipDate')} ship-from {a.get('warehouseId')} "
                  f"store {store_names.get(a.get('storeId'), a.get('storeId'))} "
                  f"tags {[tag_names.get(t, t) for t in (o.get('tagIds') or [])]} "
                  f"mergedOrSplit {a.get('mergedOrSplit')} parentId {a.get('parentId')} "
                  f"mergedIds {len(a.get('mergedIds') or [])} active {o.get('active')}")
        except Exception as e:
            print(f"    -> could not fetch: {e}")
    no_date = [o for o in shipped if not o.get("shipDate")][:4]
    for o in no_date:
        a = adv(o)
        print(f"  shipped without shipDate: order {o.get('orderNumber')} orderDate {o.get('orderDate')} "
              f"modifyDate {o.get('modifyDate')} externallyFulfilled {o.get('externallyFulfilled')} "
              f"mergedOrSplit {a.get('mergedOrSplit')} parentId {a.get('parentId')} "
              f"tags {[tag_names.get(t, t) for t in (o.get('tagIds') or [])]}")


def run_diagnose(start: date | None, end: date | None) -> int:
    ss = ShipStationClient()
    wh_names = {w["warehouseId"]: w.get("warehouseName") for w in ss._get("/warehouses")}
    print("ShipStation ship-from locations:", "; ".join(f"{k} = {v}" for k, v in wh_names.items()))
    tag_names = ss.list_tags()
    store_names = ss.list_stores()
    windows = [(start, end)] if start and end else [
        (date(2025, 3, 5), date(2025, 3, 11)),
        (date(2026, 9, 21), date(2026, 9, 27)),
    ]
    for s, e in windows:
        diagnose_window(ss, s, e, wh_names, tag_names, store_names)
    return 0


# ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--nightly", action="store_true", help="also refresh product attributes and Finale reports")
    ap.add_argument("--only", help="comma-separated sections to run")
    ap.add_argument("--diagnose", action="store_true", help="print ShipStation structure counts and exit")
    args = ap.parse_args()
    if args.diagnose:
        return run_diagnose(parse_any_date(args.start) if args.start else None,
                            parse_any_date(args.end) if args.end else None)

    today = central_today()
    end = parse_any_date(args.end) if args.end else today
    start = parse_any_date(args.start) if args.start else end - timedelta(days=3)

    sections = ["shipstation", "queue", "sales", "sheets"]
    if args.nightly or args.start:
        sections = ["attrs", "shipstation", "queue", "sales", "builds", "pos", "receipts", "sheets"]
    if args.only:
        sections = [s.strip() for s in args.only.split(",") if s.strip()]

    sb = get_supabase_client()
    if "attrs" not in sections:
        has_attrs = sb.table("kpi_product_attrs").select("product_id").limit(1).execute().data
        if not has_attrs:
            sections.insert(0, "attrs")   # first run: product attributes are required for the mix

    runners = {
        "shipstation": lambda: section_shipstation(sb, start, end),
        "queue": lambda: section_queue(sb),
        "attrs": lambda: section_attrs(sb),
        "sales": lambda: section_sales(sb, start, end),
        "builds": lambda: section_builds(sb),
        "pos": lambda: section_pos(sb),
        "receipts": lambda: section_receipts(sb),
        "sheets": lambda: section_sheets(sb),
    }
    failures = 0
    for name in sections:
        print(f"\n== {name}")
        try:
            detail = runners[name]()
            print(f"  ok: {detail}")
            log(sb, name, True, detail)
        except Exception as e:
            failures += 1
            traceback.print_exc()
            log(sb, name, False, f"{e.__class__.__name__}: {e}")
    print(f"\nDone: {len(sections) - failures}/{len(sections)} sections ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
