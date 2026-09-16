# Operations Daily Manifest — ShipStation Stats Bot

Two dashboard pages:

- **`dashboard/live.html`** — right-now numbers (Warehouse/Freight queue, today's
  running totals), calling a small local server for a genuinely live refresh
  on demand.
- **`dashboard/index.html`** ("Trends") — historical analytics: a date-range
  trend chart with toggleable metrics, plus a full data table. Built from the
  `stats.db` history captured by the twice-daily pulls.

## What it tracks

| Metric | Captured when | Definition |
|---|---|---|
| Beginning of day queue — Warehouse / Freight | 8:00 AM CST | `awaiting_shipment` orders tagged Warehouse / Freight, respectively |
| End of day queue — Warehouse / Freight | 4:30 PM CST | Same, at 4:30 PM |
| Daily orders in | 4:30 PM CST | Orders created since midnight |
| Daily orders out | 4:30 PM CST | Unique orders shipped since midnight |
| Packages shipped | 4:30 PM CST | Count of shipment records since midnight |
| Avg packages/order | 4:30 PM CST | Packages shipped ÷ orders out |

The Live page recomputes all of these (except the two queue snapshots use
current values, not historical 8am/4:30pm ones) on every refresh — see below.

**One assumption worth double-checking once real data flows in:** "packages
shipped" counts ShipStation shipment records. If your team frequently splits
one order into multiple packages *within a single shipment record*, flag it
to me and I'll adjust how it's counted.

**Tag names:** the Warehouse/Freight split relies on two ShipStation tags
named exactly `Warehouse` and `Freight`. If yours are named differently, edit
these two lines near the top of `pull_stats.py` (`live_server.py` imports
them from there, so one edit covers both):

```python
WAREHOUSE_TAG_NAME = "Warehouse"
FREIGHT_TAG_NAME = "Freight"
```

## One-time setup

```bash
cd shipstation-bot
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in your ShipStation API key + secret (found in
ShipStation under Settings → Account → API Settings):

```
SHIPSTATION_API_KEY=xxxxx
SHIPSTATION_API_SECRET=xxxxx
```

`.env` never gets committed or sent anywhere — it's read locally, server-side
only (by `pull_stats.py` and `live_server.py`). It never reaches the browser.

## Using the Live page

ShipStation's API doesn't support being called directly from a browser (no
CORS support, and their own docs advise against exposing API keys
client-side). So `live_server.py` runs locally, holds your key, and the
`live.html` page calls *that* instead — getting fresh data on every refresh
without your key ever touching the page.

```bash
python live_server.py
```

Leave that terminal open, then open `dashboard/live.html` in your browser.
It polls automatically every 20 seconds, plus a manual Refresh button. If the
page shows "Not connected," the server isn't running — start it and refresh.

You don't need to keep `live_server.py` running all the time — just when you
want to check the Live page. It's independent of the scheduled pulls below.

## Using the Trends page (historical data)

This is fed by the twice-daily scheduled pulls, not the live server.

```bash
python pull_stats.py morning
python pull_stats.py evening
```

Each run updates `stats.db` and regenerates `dashboard/index.html`
automatically. Open `dashboard/index.html` anytime to browse trends — no
server needed, it's a self-contained file.

## Automating the daily pulls (macOS/Linux — cron)

Run `crontab -e` and add (adjust the path to wherever you put this folder):

```cron
# ShipStation morning pull — 8:00 AM weekdays, Central Time
0 8 * * 1-5 cd /path/to/shipstation-bot && /usr/bin/python3 pull_stats.py morning >> pull.log 2>&1

# ShipStation evening pull — 4:30 PM weekdays, Central Time
30 16 * * 1-5 cd /path/to/shipstation-bot && /usr/bin/python3 pull_stats.py evening >> pull.log 2>&1
```

Note: cron uses your **system's local timezone**. If your machine isn't set
to Central time, adjust the hours accordingly, or prefix the crontab with
`CRON_TZ=America/Chicago` (supported on most modern cron implementations).

Your machine needs to be on and awake at those times for cron to fire.

## Automating the daily pulls (Windows — Task Scheduler)

1. Open Task Scheduler → Create Basic Task
2. Trigger: Weekly, Mon–Fri, 8:00 AM (repeat a second task for 4:30 PM)
3. Action: Start a program
   - Program: `python.exe` (or full path to it)
   - Arguments: `pull_stats.py morning` (or `evening` for the afternoon task)
   - Start in: the full path to this `shipstation-bot` folder

## Running the Live server automatically (optional)

If you want `live_server.py` running whenever your machine is on, rather
than starting it manually each time, set up a Task Scheduler / cron entry
that runs `python live_server.py` at login, with no end trigger. It's
lightweight and idles quietly between refreshes.

## Adding more scheduled pulls per day

If you find certain data needs a third pull (e.g., a midday check), add
another `if sys.argv[1] == "..."` branch in `pull_stats.py` and a matching
cron line — the SQLite schema already supports partial updates per day.

## Files

```
shipstation-bot/
├── .env                  # your API credentials (create from .env.example)
├── pull_stats.py         # cron-called script — pulls data, updates stats.db
├── live_server.py        # local proxy server for the Live page
├── shipstation_client.py # ShipStation API wrapper
├── build_dashboard.py    # regenerates dashboard/index.html from stats.db
├── stats.db              # SQLite history — one row per day, growing over time
├── dashboard/
│   ├── template.html     # Trends page design/logic (edit this to restyle)
│   ├── index.html        # auto-generated Trends page — open in browser
│   └── live.html         # Live page — open in browser (needs live_server.py running)
└── seed_mock_data.py     # optional: fills stats.db with fake data to preview
                           # the Trends page before your API key is wired up
```

## Preview with fake data

Before your first real pull runs, you can see what the Trends page looks
like:

```bash
python seed_mock_data.py
python build_dashboard.py
```

Then open `dashboard/index.html`. Delete `stats.db` before going live so
real data starts fresh (or just let the real pulls overwrite fake rows —
today's row gets overwritten by the real pull once it runs).
