#!/usr/bin/env python3
"""
Local web server for the live queue dashboard. Reads live_queue.csv fresh
on every request — it does NOT talk to Finale or ShipStation itself; that
work is already done by pull_live_queue.py running on its own 5-minute
schedule via Task Scheduler. This server's only job is to read whatever
that script most recently wrote and present it.

Run this once and leave it running (e.g. as its own Task Scheduler task
set to start at logon, or just start it manually each morning):

    python live_server.py

Then open http://localhost:5057 in a browser and leave the tab open —
the page polls this server every 30 seconds and updates itself, so the
underlying CSV refreshing every 5 minutes shows up automatically without
ever needing to reload the page by hand.
"""
import csv
import os
from datetime import datetime, date

from flask import Flask, jsonify, render_template

app = Flask(__name__)

QUEUE_CSV_PATH = "live_queue.csv"


def read_queue_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def order_age_days(order_date_str: str) -> int | None:
    """Days since the order was placed, for spotting stuck/aged orders."""
    if not order_date_str:
        return None
    try:
        order_date = datetime.strptime(order_date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - order_date).days


def build_summary(rows: list[dict]) -> dict:
    """Rows are one per (order x product line) — Core Queue and Order
    date are the same across every line of a given order, so we dedupe to
    the order level before counting "how many orders," while category-type
    breakdowns stay at the line level (a single order can span more than
    one category)."""
    orders: dict[str, dict] = {}
    for row in rows:
        oid = row.get("Order ID", "")
        if oid not in orders:
            orders[oid] = row

    core_queue_counts: dict[str, int] = {}
    oldest_age = 0
    oldest_order_id = None
    for oid, row in orders.items():
        cq = row.get("Core Queue", "") or "Other"
        core_queue_counts[cq] = core_queue_counts.get(cq, 0) + 1
        age = order_age_days(row.get("Order date", ""))
        if age is not None and age > oldest_age:
            oldest_age = age
            oldest_order_id = oid

    def line_counts(field: str) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in rows:
            value = row.get(field, "").strip() or "(none)"
            counts[value] = counts.get(value, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    return {
        "total_orders": len(orders),
        "core_queue_counts": core_queue_counts,
        "oldest_age_days": oldest_age,
        "oldest_order_id": oldest_order_id,
        "category_counts": line_counts("Category"),
        "chem_tech_counts": line_counts("CHEM TECH"),
        "lab_room_counts": line_counts("LAB ROOM"),
    }


@app.route("/")
def index():
    return render_template("live.html")


@app.route("/api/queue")
def api_queue():
    rows = read_queue_rows(QUEUE_CSV_PATH)
    last_updated = None
    if os.path.exists(QUEUE_CSV_PATH):
        last_updated = datetime.fromtimestamp(os.path.getmtime(QUEUE_CSV_PATH)).strftime("%I:%M:%S %p")
    return jsonify({
        "last_updated": last_updated,
        "summary": build_summary(rows),
        "rows": rows,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
