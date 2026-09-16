#!/usr/bin/env python3
"""
Reads stats.db and regenerates dashboard/index.html as a fully self-contained
HTML file (data embedded inline, no server/fetch needed — just double-click
to open it in a browser). Run automatically at the end of every pull_stats.py
run, or manually: `python build_dashboard.py`.
"""
import os
import json
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "stats.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "dashboard")
OUT_PATH = os.path.join(OUT_DIR, "index.html")
TEMPLATE_PATH = os.path.join(OUT_DIR, "template.html")


def load_rows():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM daily_stats ORDER BY date ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build():
    rows = load_rows()
    with open(TEMPLATE_PATH, "r") as f:
        template = f.read()

    html = template.replace("__STATS_JSON__", json.dumps(rows))

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard rebuilt: {OUT_PATH} ({len(rows)} day(s) of data)")


if __name__ == "__main__":
    build()
