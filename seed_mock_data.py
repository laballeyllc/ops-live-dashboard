"""One-off helper: populates stats.db with plausible fake data so you can
preview the dashboard before hooking up the real API key. Not part of the
daily automation — delete stats.db (or re-run pull_stats.py for real) once
you're ready to go live."""
import random
import sqlite3
import os
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "stats.db")

conn = sqlite3.connect(DB_PATH)
conn.execute("""
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,
    beginning_queue INTEGER,
    end_queue INTEGER,
    orders_in INTEGER,
    orders_out INTEGER,
    packages_shipped INTEGER,
    avg_packages_per_order REAL,
    last_updated TEXT,
    PRIMARY KEY (date)
);
""")

random.seed(7)
queue = 140
today = date.today()
start = today - timedelta(days=27)

d = start
while d <= today:
    if d.weekday() < 5:  # weekdays only
        orders_in = random.randint(180, 260)
        orders_out = random.randint(170, 270)
        beginning_queue = queue
        end_queue = max(20, beginning_queue + orders_in - orders_out)
        packages_shipped = int(orders_out * random.uniform(1.05, 1.35))
        avg_pkgs = round(packages_shipped / orders_out, 2) if orders_out else 0
        conn.execute(
            "INSERT OR REPLACE INTO daily_stats (date, beginning_queue, end_queue, orders_in, orders_out, packages_shipped, avg_packages_per_order, last_updated) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (d.isoformat(), beginning_queue, end_queue, orders_in, orders_out, packages_shipped, avg_pkgs)
        )
        queue = end_queue
    d += timedelta(days=1)

conn.commit()
conn.close()
print("Seeded mock data.")
