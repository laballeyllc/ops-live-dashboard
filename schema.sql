-- Run this in Supabase: Project -> SQL Editor -> New query -> paste -> Run
--
-- Two tables, matching the two jobs we already have:
--   snapshots   — append-only historical record (replaces ops_history.db).
--                 Every run of pull_ops_data.py ADDS rows here; nothing
--                 is ever deleted or overwritten.
--   live_queue  — the current queue (replaces live_queue.csv). Every run
--                 of pull_live_queue.py REPLACES the whole table's
--                 contents, since this represents "right now," not history.

create table if not exists snapshots (
  id bigint generated always as identity primary key,
  pulled_at timestamptz not null,
  order_id text,
  order_date text,
  product_id text,
  description text,
  category text,
  chem text,
  chem_tech text,
  lab_room text,
  shipment_id text,
  ship_date_actual text,
  shipment_status text,
  tags text,
  core_queue text
);

create table if not exists live_queue (
  id bigint generated always as identity primary key,
  pulled_at timestamptz not null,
  order_id text,
  order_date text,
  product_id text,
  description text,
  category text,
  chem text,
  chem_tech text,
  lab_room text,
  shipment_status text,
  tags text,
  core_queue text
);

-- Helpful indexes for the frontend's queries (filtering/sorting by these).
create index if not exists snapshots_pulled_at_idx on snapshots (pulled_at);
create index if not exists snapshots_order_id_idx on snapshots (order_id);
create index if not exists live_queue_core_queue_idx on live_queue (core_queue);

-- Row Level Security: the anon/public key (used by the Netlify frontend,
-- visible to anyone who views the page source) can ONLY read. It cannot
-- insert, update, or delete anything on either table. The service role
-- key (used only by GitHub Actions, kept in a Secret, never shipped to
-- the browser) bypasses RLS entirely by design, so the write scripts
-- don't need any policy at all — only the read-only side needs one.

alter table snapshots enable row level security;
alter table live_queue enable row level security;

create policy "Public read access" on snapshots
  for select using (true);

create policy "Public read access" on live_queue
  for select using (true);
