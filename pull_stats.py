#!/usr/bin/env python3
"""
Pulls ShipStation stats and stores them in stats.db.

Usage:
    python pull_stats.py morning     # 8:00 AM run — beginning-of-day queue
    python pull_stats.py evening     # 4:30 PM run — end-of-day queue + daily totals

Designed to be called by cron/Task Scheduler. Safe to call more than once for
the same run_type + date — it will overwrite (upsert) that day's value rather
than duplicate rows.
"""
import os
import sys
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from shipstation_client import ShipStationClient

TZ = ZoneInfo("America/Chicago")
DB_PATH = os.path.join(os.path.dirname(__file__), "stats.db")

# Exact ShipStation tag names for each queue (Settings > Automation > Tags).
# Edit these two if your tags are named differently.
WAREHOUSE_LOCATION_NAME = "Dripping Springs Warehouse"
FREIGHT_LOCATION_NAME = "Dripping Springs Freight"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,                     -- YYYY-MM-DD (business date, CST)
    beginning_queue_warehouse INTEGER,      -- Warehouse-tagged awaiting_shipment @ 8am
    beginning_queue_freight INTEGER,        -- Freight-tagged awaiting_shipment @ 8am
    end_queue_warehouse INTEGER,            -- Warehouse-tagged awaiting_shipment @ 4:30pm
    end_queue_freight INTEGER,              -- Freight-tagged awaiting_shipment @ 4:30pm
    orders_in INTEGER,                      -- orders created today (as of evening pull)
    orders_out INTEGER,                     -- orders shipped today
    packages_shipped INTEGER,               -- shipment count today
    avg_packages_per_order REAL,            -- packages_shipped / orders_out
    last_updated TEXT,                      -- ISO timestamp of last write
    PRIMARY KEY (date)
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def upsert_row(conn, date, **fields):
    """Insert a row for `date` if absent, otherwise update only the given fields."""
    cur = conn.execute("SELECT 1 FROM daily_stats WHERE date = ?", (date,))
    exists = cur.fetchone() is not None
    fields["last_updated"] = datetime.now(TZ).isoformat()

    if not exists:
        cols = ["date"] + list(fields.keys())
        placeholders = ", ".join(["?"] * len(cols))
        values = [date] + list(fields.values())
        conn.execute(
            f"INSERT INTO daily_stats ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
    else:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [date]
        conn.execute(
            f"UPDATE daily_stats SET {set_clause} WHERE date = ?",
            values,
        )
    conn.commit()


def ss_datetime(dt: datetime) -> str:
    """Format a datetime the way ShipStation's API expects: MM/DD/YYYY HH:MM"""
    return dt.strftime("%m/%d/%Y %H:%M")


def get_queue_counts(client: ShipStationClient):
    """Returns (warehouse_queue, freight_queue) counts of awaiting_shipment
    orders whose Ship From Location is Warehouse / Freight respectively."""
    warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)

    warehouse_queue = client.count_by_warehouse(warehouse_id, order_status="awaiting_shipment")
    freight_queue = client.count_by_warehouse(freight_id, order_status="awaiting_shipment")
    return warehouse_queue, freight_queue


def run_morning(client: ShipStationClient, conn):
    """8:00 AM CST — snapshot the beginning-of-day queue, by tag."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    warehouse_queue, freight_queue = get_queue_counts(client)

    upsert_row(
        conn, today,
        beginning_queue_warehouse=warehouse_queue,
        beginning_queue_freight=freight_queue,
    )
    print(f"[{today}] morning pull complete — warehouse={warehouse_queue}, freight={freight_queue}")


def run_evening(client: ShipStationClient, conn):
    """4:30 PM CST — snapshot end-of-day queue + compute the day's totals."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    warehouse_id = client.get_warehouse_id(WAREHOUSE_LOCATION_NAME)
    freight_id = client.get_warehouse_id(FREIGHT_LOCATION_NAME)
    location_ids = [warehouse_id, freight_id]

    warehouse_queue, freight_queue = get_queue_counts(client)

    # Scoped to just the Warehouse + Freight locations AND to orders still
    # sitting in awaiting_shipment (not on_hold, awaiting_payment, etc.) —
    # otherwise this counts activity that never entered the queue at all,
    # which breaks the beginning + in - out = end math.
    orders_in = client.count_by_warehouses(
        location_ids,
        order_status="awaiting_shipment",
        create_date_start=ss_datetime(day_start),
        create_date_end=ss_datetime(now),
    )

    shipments_today = client.list_shipments_by_warehouses(
        location_ids,
        ship_date_start=ss_datetime(day_start),
        ship_date_end=ss_datetime(now),
    )
    packages_shipped = len(shipments_today)

    # Count unique orders behind today's shipments (so a 3-package order
    # counts once, not three times). Prefer orderId; if that field isn't
    # populated for this account's shipment records, fall back to
    # orderNumber rather than silently collapsing to packages_shipped.
    unique_order_keys = {
        s.get("orderId") or s.get("orderNumber")
        for s in shipments_today
        if s.get("orderId") or s.get("orderNumber")
    }
    orders_out = len(unique_order_keys) if unique_order_keys else packages_shipped

    avg_packages_per_order = round(packages_shipped / orders_out, 2) if orders_out else 0.0

    upsert_row(
        conn, today,
        end_queue_warehouse=warehouse_queue,
        end_queue_freight=freight_queue,
        orders_in=orders_in,
        orders_out=orders_out,
        packages_shipped=packages_shipped,
        avg_packages_per_order=avg_packages_per_order,
    )
    print(
        f"[{today}] evening pull complete — warehouse_queue={warehouse_queue}, freight_queue={freight_queue}, "
        f"orders_in={orders_in}, orders_out={orders_out}, packages_shipped={packages_shipped}, "
        f"avg_packages_per_order={avg_packages_per_order}"
    )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("morning", "evening"):
        print("Usage: python pull_stats.py [morning|evening]")
        sys.exit(1)

    load_dotenv()
    client = ShipStationClient()
    conn = get_db()

    if sys.argv[1] == "morning":
        run_morning(client, conn)
    else:
        run_evening(client, conn)

    conn.close()

    import build_dashboard
    build_dashboard.build()


if __name__ == "__main__":
    main()
