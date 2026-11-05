"""Supplier pricing pull: Finale report "SCM_Purchase order w/ detail" -> supplier_prices.

One row per RECEIVED or COMPLETED purchase-order line, with the PO line's unit
price (the price at the time of purchase), the order date (the time axis) and
the destination (so the page can filter by it). Runs once
a day from .github/workflows/supplier-prices.yml; the supplier-pricing.html
page reads the table with the public key.

What counts as a row (decided with Casey, Nov 4):
  - the PO line was received ("Quantity received" above 0; a partly received
    line counts, its price is still the PO line's price) OR the PO is
    Completed. Dropship POs are never received (the supplier ships straight
    to the customer) but Finale marks them Completed, so they are kept and
    the page's Destination filter can include or exclude them.
  - the PO is not canceled or a draft
  - it has a supplier, a product ID, a valid order date and a unit price above 0
  - the order date is within the last 731 days (about two years)

Needs: FINALE_API_KEY, FINALE_API_SECRET, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
and the report address in FINALE_SUPPLIER_PRICES_REPORT_URL.

Test without touching anything:
  python pull_supplier_prices.py --csv report.csv --dry-run
(--csv reads a saved copy of the report instead of calling Finale; --dry-run
skips the database.)

Only long-standing basics are borrowed from ops_common, and everything else
lives here, so changes to Ops Live or the KPI sync can't break this pull.
"""
import argparse
import csv
import hashlib
import re
import sys
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:          # python-dotenv is only a convenience for local runs
    pass

HISTORY_DAYS = 731           # about two years
BATCH = 500
DELETE_BATCH = 200
TABLE = "supplier_prices"
EXCLUDED_STATUSES = {"canceled", "cancelled", "draft"}
COMPLETED_STATUSES = {"completed", "complete"}
# If a run would delete more than this share of what is already stored, it
# stops instead: a bad or half-loaded report must never empty the table.
MAX_DELETE_SHARE = 0.5

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y")


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def norm_key(k) -> str:
    return re.sub(r"\s+", " ", str(k)).strip().lower()


def resolve_columns(rows: list, spec: dict) -> dict:
    """Finds each needed column by keywords instead of exact names (Finale
    headers vary a little). spec: field -> alternatives; each alternative is
    a tuple of words that must all appear in the header; a word starting
    with '!' must NOT appear. First alternative that matches wins."""
    columns = []
    for r in rows[:200]:
        for k in r.keys():
            if k not in columns:
                columns.append(k)
    out = {}
    for field, alternatives in spec.items():
        out[field] = None
        for words in alternatives:
            hit = next((c for c in columns
                        if all((w[1:] not in norm_key(c)) if w.startswith("!") else (w in norm_key(c)) for w in words)),
                       None)
            if hit:
                out[field] = hit
                break
    return out


def describe(mapping: dict, rows: list) -> str:
    cols = []
    for r in rows[:200]:
        for k in r.keys():
            if k not in cols:
                cols.append(k)
    found = ", ".join(f"{f}='{c}'" for f, c in mapping.items())
    return f"columns used: {found} | all columns: {[norm_key(c) for c in cols]}"


def parse_any_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
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
    """'$1,234.50' -> 1234.5; '--', blank or text -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if not s or s == "--":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean(value) -> str:
    s = "" if value is None else str(value).strip()
    return "" if s == "--" else s


def is_total_row(r: dict) -> bool:
    return any(str(v).strip().upper().startswith("TOTAL") for v in r.values() if isinstance(v, str))


# ---------------------------------------------------------------------
# The rules: report rows -> supplier_prices rows
# ---------------------------------------------------------------------

COLUMN_SPEC = {
    "order_date": [("order", "date")],
    "po_id":      [("order", "id")],
    "status":     [("order", "status")],
    "supplier":   [("supplier",), ("vendor",)],
    "product":    [("product", "id")],
    "description": [("description",)],
    "category":   [("category",)],
    "destination": [("destination",)],
    "qty":        [("quantity", "!receiv")],
    "received":   [("quantity", "receiv"), ("receiv", "!status", "!date")],
    "price":      [("unit", "price")],
    "landed":     [("landed", "per unit")],
}
REQUIRED = ["order_date", "po_id", "supplier", "product", "price", "received"]


def transform(rows: list, today: date = None, history_days: int = HISTORY_DAYS, now: datetime = None):
    """Returns (kept rows, stats). Pure function: no network, no database."""
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    cutoff = today - timedelta(days=history_days)
    m = resolve_columns(rows, COLUMN_SPEC)
    missing = [f for f in REQUIRED if not m.get(f)]
    if missing:
        raise RuntimeError(f"Supplier pricing report: couldn't find column(s) for {missing}. {describe(m, rows)}")

    skipped = Counter()
    seen = Counter()
    kept = []
    for r in rows:
        if is_total_row(r):
            skipped["total row"] += 1
            continue
        pid, po, supplier = clean(r.get(m["product"])), clean(r.get(m["po_id"])), clean(r.get(m["supplier"]))
        status = clean(r.get(m["status"])) if m["status"] else ""
        if not pid:
            skipped["no product ID"] += 1
            continue
        if not supplier:
            skipped["no supplier"] += 1
            continue
        if status.lower() in EXCLUDED_STATUSES:
            skipped["canceled or draft PO"] += 1
            continue
        order_date = parse_any_date(r.get(m["order_date"]))
        if not order_date:
            skipped["bad order date"] += 1
            continue
        if order_date < cutoff:
            skipped["older than the history window"] += 1
            continue
        price = to_number(r.get(m["price"]))
        if price is None or price <= 0:
            skipped["no unit price"] += 1
            continue
        received = to_number(r.get(m["received"]))
        completed = status.lower() in COMPLETED_STATUSES
        if (received is None or received <= 0) and not completed:
            skipped["not received and not completed"] += 1
            continue
        qty = to_number(r.get(m["qty"])) if m["qty"] else None
        landed = to_number(r.get(m["landed"])) if m["landed"] else None
        # A PO can list the same product twice with identical numbers; the
        # occurrence counter keeps both lines instead of collapsing them.
        base = f"{po}|{pid}|{order_date.isoformat()}|{price}|{qty}"
        occurrence = seen[base]
        seen[base] += 1
        kept.append({
            "line_key": hashlib.md5(f"{base}|{occurrence}".encode()).hexdigest(),
            "po_id": po,
            "order_date": order_date.isoformat(),
            "supplier": supplier,
            "product_id": pid,
            "description": clean(r.get(m["description"])) if m["description"] else None,
            "category": clean(r.get(m["category"])) if m["category"] else None,
            "destination": (clean(r.get(m["destination"])) or None) if m["destination"] else None,
            "order_status": status or None,
            "quantity": qty,
            "quantity_received": received if received is not None else 0.0,
            "unit_price": price,
            "landed_cost_per_unit": landed if landed else None,
            "updated_at": now.isoformat(),      # the page shows "prices as of" from the newest of these
        })
    stats = {"read": len(rows), "kept": len(kept), "skipped": dict(skipped), "mapping": m}
    return kept, stats


# ---------------------------------------------------------------------
# Keeping the table in step with the report
# ---------------------------------------------------------------------

class SupabaseStore:
    def __init__(self, client):
        self.client = client

    def existing_keys(self) -> set:
        keys, start = set(), 0
        while True:
            res = (self.client.table(TABLE).select("line_key").order("line_key")
                   .range(start, start + 999).execute())
            batch = res.data or []
            keys.update(r["line_key"] for r in batch)
            if len(batch) < 1000:
                return keys
            start += 1000

    def upsert(self, rows: list) -> None:
        for i in range(0, len(rows), BATCH):
            self.client.table(TABLE).upsert(rows[i:i + BATCH], on_conflict="line_key").execute()

    def delete_keys(self, keys: list) -> None:
        for i in range(0, len(keys), DELETE_BATCH):
            self.client.table(TABLE).delete().in_("line_key", keys[i:i + DELETE_BATCH]).execute()


def sync(store, rows: list) -> dict:
    """Writes the current rows, then removes stored rows that are no longer in
    the report (a PO later canceled, corrected, or aged out of the window)."""
    if not rows:
        raise RuntimeError("Nothing to write: the report produced no usable rows, so the table was left alone.")
    existing = store.existing_keys()
    current = {r["line_key"] for r in rows}
    stale = sorted(existing - current)
    if existing and len(stale) > MAX_DELETE_SHARE * len(existing):
        raise RuntimeError(
            f"Refusing to delete {len(stale)} of {len(existing)} stored rows (more than "
            f"{int(MAX_DELETE_SHARE * 100)}%). The report looks incomplete; the table was left alone. "
            "If this is really intended, empty supplier_prices in Supabase and run again.")
    store.upsert(rows)
    store.delete_keys(stale)
    return {"written": len(rows), "new": len(current - existing), "removed": len(stale)}


# ---------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------

def read_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    return list(csv.DictReader(text.splitlines(), dialect=dialect))


def load_from_finale() -> list:
    import os
    url = os.environ.get("FINALE_SUPPLIER_PRICES_REPORT_URL", "").strip()
    if not url:
        raise RuntimeError("FINALE_SUPPLIER_PRICES_REPORT_URL is not set (GitHub: Settings > Secrets and variables > Actions > Variables).")
    from ops_common import fetch_finale_report
    url = url.replace("/pivotTableStream/", "/pivotTable/")   # the Stream form returns a web page, not data
    return fetch_finale_report(url)


def log_run(client, ok: bool, detail: str) -> None:
    try:
        client.table("kpi_sync_log").insert({"section": "supplier_prices", "ok": ok, "detail": detail[:2000]}).execute()
    except Exception as e:     # logging must never take the run down
        print(f"  (could not write kpi_sync_log: {e})")


def summarize(kept: list, stats: dict) -> str:
    skipped = ", ".join(f"{k}: {v}" for k, v in sorted(stats["skipped"].items(), key=lambda kv: -kv[1])) or "none"
    suppliers = len({r["supplier"] for r in kept})
    skus = len({r["product_id"] for r in kept})
    span = f"{min(r['order_date'] for r in kept)} to {max(r['order_date'] for r in kept)}" if kept else "n/a"
    return (f"{stats['read']} report rows; kept {stats['kept']} received or completed lines "
            f"({skus} SKUs, {suppliers} suppliers, orders {span}); skipped [{skipped}]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", help="read a saved copy of the report instead of calling Finale")
    ap.add_argument("--dry-run", action="store_true", help="work out the rows but do not touch the database")
    ap.add_argument("--history-days", type=int, default=HISTORY_DAYS)
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args(argv)

    client = None
    try:
        rows = read_csv(args.csv) if args.csv else load_from_finale()
        today = parse_any_date(args.today) if args.today else None
        kept, stats = transform(rows, today=today, history_days=args.history_days)
        summary = summarize(kept, stats)
        print(summary)
        if not kept:
            raise RuntimeError(f"No usable rows. {describe(stats['mapping'], rows)}")
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        from ops_common import get_supabase_client
        client = get_supabase_client()
        result = sync(SupabaseStore(client), kept)
        detail = f"{summary}; wrote {result['written']} ({result['new']} new), removed {result['removed']}"
        print(detail)
        log_run(client, True, detail)
        return 0
    except Exception as e:
        traceback.print_exc()
        if client is not None:
            log_run(client, False, f"{e.__class__.__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
