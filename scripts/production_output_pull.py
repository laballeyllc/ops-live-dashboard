"""
Lab Alley: log what production completed, for the wall displays.

Writes two kinds of rows to Supabase `production_output`:
  finale_build    one row per product on each completed Finale build
                  (units and build count, from a Finale report)
  stock_increase  any rise in on-hand stock of built SKUs since the last
                  run, read from the stock_levels table Ops Live already fills

Run it on a schedule during the shift, right after the Stock Levels Pull.
Safe to re-run: builds are upserted by (build ID, product).

Environment variables (GitHub Actions secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  FINALE_BUILDS_REPORT_URL   CSV export URL of a Finale report listing
                             completed builds (see README)
  FINALE_API_KEY, FINALE_API_SECRET   Finale API credentials
Optional, if your report's column headers differ:
  BUILD_ID_COL (default "Build ID"), BUILD_PRODUCT_COL ("Product ID"),
  BUILD_QTY_COL ("Quantity"), BUILD_COMPLETED_COL ("Completed date")
"""
import csv
import io
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

CENTRAL = ZoneInfo("America/Chicago")
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SB_HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}

COL_ID = os.environ.get("BUILD_ID_COL", "Build ID")
COL_PRODUCT = os.environ.get("BUILD_PRODUCT_COL", "Product ID")
COL_QTY = os.environ.get("BUILD_QTY_COL", "Quantity")
COL_DONE = os.environ.get("BUILD_COMPLETED_COL", "Completed date")


def sb_select_all(table, select="*", params=None):
    rows, start, page = [], 0, 1000
    while True:
        h = dict(SB_HEADERS, Range=f"{start}-{start + page - 1}")
        r = requests.get(f"{SB_URL}/rest/v1/{table}", headers=h, params=dict(params or {}, select=select), timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            return rows
        start += page


def sb_upsert(table, rows, on_conflict):
    for i in range(0, len(rows), 500):
        h = dict(SB_HEADERS, Prefer="resolution=merge-duplicates,return=minimal")
        r = requests.post(f"{SB_URL}/rest/v1/{table}", headers=h, params={"on_conflict": on_conflict},
                          json=rows[i:i + 500], timeout=60)
        r.raise_for_status()


def parse_number(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_central(v):
    """Finale report dates are local (Central). Accepts common formats."""
    v = (v or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%b %d %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.replace(tzinfo=CENTRAL).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def pull_finale_builds():
    url = os.environ.get("FINALE_BUILDS_REPORT_URL")
    if not url:
        print("FINALE_BUILDS_REPORT_URL not set; skipping completed builds")
        return 0
    r = requests.get(url, auth=(os.environ["FINALE_API_KEY"], os.environ["FINALE_API_SECRET"]), timeout=120)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
    missing = [c for c in (COL_ID, COL_PRODUCT, COL_QTY, COL_DONE) if c not in (reader.fieldnames or [])]
    if missing:
        sys.exit(f"Finale builds report is missing columns {missing}. Found: {reader.fieldnames}")
    merged = {}
    for row in reader:
        done = parse_central(row[COL_DONE])
        build_id, product = row[COL_ID].strip(), row[COL_PRODUCT].strip()
        if not (done and build_id and product):
            continue
        key = (build_id, product)
        if key not in merged:
            merged[key] = {"source": "finale_build", "ref_id": build_id, "product_id": product,
                           "units": 0, "occurred_at": done.isoformat()}
        merged[key]["units"] += parse_number(row[COL_QTY])
    sb_upsert("production_output", list(merged.values()), "source,ref_id,product_id")
    print(f"Completed builds: upserted {len(merged)} build lines")
    return len(merged)


def log_stock_increases():
    current = sb_select_all("stock_levels", "product_id,usable_stock,is_built", {"is_built": "eq.true"})
    last = {r["product_id"]: float(r["units"]) for r in sb_select_all("built_stock_last", "product_id,units")}
    now = datetime.now(timezone.utc)
    ref = now.strftime("%Y-%m-%dT%H:%M")
    increases, snapshot = [], []
    for s in current:
        pid, units = s["product_id"], parse_number(s.get("usable_stock"))
        snapshot.append({"product_id": pid, "units": units, "seen_at": now.isoformat()})
        if pid in last and units > last[pid]:
            increases.append({"source": "stock_increase", "ref_id": ref, "product_id": pid,
                              "units": round(units - last[pid], 4), "occurred_at": now.isoformat()})
    if increases:
        sb_upsert("production_output", increases, "source,ref_id,product_id")
    sb_upsert("built_stock_last", snapshot, "product_id")
    first = " (first run: baseline recorded, increases start next run)" if not last else ""
    print(f"Stock increases: {len(increases)} built SKUs went up{first}")


if __name__ == "__main__":
    pull_finale_builds()
    log_stock_increases()
