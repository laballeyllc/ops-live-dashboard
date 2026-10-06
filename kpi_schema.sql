-- KPI Summary tool: tables, view, and the kpi_summary() function.
-- Run this once in the Supabase SQL editor for the Ops Live project
-- (pdflsmequzgpeuodnxyg). Safe to re-run: everything is "if not exists"
-- or "create or replace".
--
-- Written to by pull_kpi_data.py (GitHub Actions, service role key).
-- Read by site/kpi.html (anon key) through kpi_summary() and two views.

-- ---------------------------------------------------------------------
-- ShipStation facts
-- ---------------------------------------------------------------------
create table if not exists kpi_orders (
  order_id        bigint primary key,      -- ShipStation internal orderId
  order_number    text,
  queue           text,                    -- 'Warehouse' | 'Freight' | '' (other ship-from)
  status          text,                    -- ShipStation orderStatus
  order_dt        timestamptz,             -- real instant (parsed from Pacific naive)
  order_date      date,                    -- Central calendar date
  ship_date       date,                    -- ShipStation shipDate (date only)
  ship_dt         timestamptz,             -- label time, or 4 PM Central on ship_date
  ship_dt_source  text,                    -- 'label' | 'end_of_shift'
  queue_bhours    numeric,                 -- business hours order_dt -> ship_dt (weekends excluded)
  units           integer,
  items           jsonb,                   -- [{"sku": "...", "qty": n}]
  updated_at      timestamptz default now()
);
create index if not exists kpi_orders_order_date_idx on kpi_orders (order_date);
create index if not exists kpi_orders_ship_date_idx  on kpi_orders (ship_date);

create table if not exists kpi_shipments (
  shipment_id  bigint primary key,
  order_id     bigint,
  queue        text,
  ship_date    date,
  create_dt    timestamptz,
  voided       boolean default false
);
create index if not exists kpi_shipments_ship_date_idx on kpi_shipments (ship_date);
create index if not exists kpi_shipments_order_idx     on kpi_shipments (order_id);

-- One row per product per day: units sitting in the Warehouse/Freight
-- queue at the day's last sync. Averaged across a range for "queue mix".
create table if not exists kpi_queue_daily (
  snap_date   date,
  product_id  text,
  units       numeric,
  primary key (snap_date, product_id)
);

-- ---------------------------------------------------------------------
-- Finale facts
-- ---------------------------------------------------------------------
create table if not exists kpi_product_attrs (
  product_id  text primary key,
  category    text,
  chem        text,
  lab_room    text,
  downpack    text,
  hazmat      text,
  updated_at  timestamptz default now()
);

create table if not exists kpi_sales_daily (
  sale_date  date primary key,
  units      numeric,
  dollars    numeric
);

create table if not exists kpi_builds (
  build_id       text primary key,
  product_id     text,
  quantity       numeric,
  complete_date  date
);
create index if not exists kpi_builds_date_idx on kpi_builds (complete_date);

create table if not exists kpi_purchase_orders (
  po_id       text primary key,
  supplier    text,
  order_date  date,
  value       numeric
);
create index if not exists kpi_po_date_idx on kpi_purchase_orders (order_date);

create table if not exists kpi_receipts (
  receipt_key   text primary key,
  product_id    text,
  supplier      text,
  order_date    date,
  receive_date  date,
  lead_days     numeric
);
create index if not exists kpi_receipts_date_idx on kpi_receipts (receive_date);

-- Editable from the KPI page: which suppliers are imports.
create table if not exists kpi_supplier_origin (
  supplier    text primary key,
  origin      text not null check (origin in ('domestic', 'import')),
  updated_at  timestamptz default now()
);

-- ---------------------------------------------------------------------
-- Google Sheets facts
-- ---------------------------------------------------------------------
create table if not exists kpi_cycle_counts (
  count_week  date,
  location    text,
  accuracy    numeric,          -- fraction, 0.9047 = 90.47%
  primary key (count_week, location)
);

create table if not exists kpi_products_added (
  row_key  text primary key,
  week_of  date,
  product  text,
  grade    text,
  la_sku   text
);
create index if not exists kpi_products_added_week_idx on kpi_products_added (week_of);

create table if not exists kpi_sync_log (
  id      bigint generated always as identity primary key,
  run_at  timestamptz default now(),
  section text,
  ok      boolean,
  detail  text
);

-- ---------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------
-- Daily inventory value, from the turnover snapshots Ops Live already logs.
create or replace view kpi_inventory_daily as
  select left(pulled_at::text, 10)::date as snap_date,
         sum(total_value)                as total_value,
         sum(units_on_hand)              as units
  from turnover_snapshots
  group by 1;

-- Every supplier seen on a receipt or PO, with its origin if set.
create or replace view kpi_supplier_list as
  select s.supplier, o.origin
  from (
    select distinct trim(supplier) as supplier from kpi_receipts where coalesce(trim(supplier), '') <> ''
    union
    select distinct trim(supplier) from kpi_purchase_orders where coalesce(trim(supplier), '') <> ''
  ) s
  left join kpi_supplier_origin o on lower(o.supplier) = lower(s.supplier)
  order by 1;

-- ---------------------------------------------------------------------
-- Row level security: public read; supplier origin also writable.
-- ---------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['kpi_orders','kpi_shipments','kpi_queue_daily','kpi_product_attrs',
                           'kpi_sales_daily','kpi_builds','kpi_purchase_orders','kpi_receipts',
                           'kpi_supplier_origin','kpi_cycle_counts','kpi_products_added','kpi_sync_log']
  loop
    execute format('alter table %I enable row level security', t);
    if not exists (select 1 from pg_policies where tablename = t and policyname = 'kpi public read') then
      execute format('create policy "kpi public read" on %I for select using (true)', t);
    end if;
  end loop;
end $$;

do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'kpi_supplier_origin' and policyname = 'kpi origin insert') then
    create policy "kpi origin insert" on kpi_supplier_origin for insert with check (true);
  end if;
  if not exists (select 1 from pg_policies where tablename = 'kpi_supplier_origin' and policyname = 'kpi origin update') then
    create policy "kpi origin update" on kpi_supplier_origin for update using (true) with check (true);
  end if;
end $$;

-- ---------------------------------------------------------------------
-- kpi_summary(start, end): every KPI for one inclusive date range.
-- ---------------------------------------------------------------------
create or replace function kpi_summary(p_start date, p_end date)
returns jsonb
language sql
stable
as $$
with
today as (select (now() at time zone 'America/Chicago')::date as d),
span as (
  select greatest(1, (least(p_end, (select d from today)) - p_start + 1)) as days
),
o_in as (
  select * from kpi_orders
  where order_date between p_start and p_end
    and queue in ('Warehouse', 'Freight')
    and coalesce(status, '') <> 'cancelled'
),
o_out as (
  select * from kpi_orders
  where ship_date between p_start and p_end
    and queue in ('Warehouse', 'Freight')
    and status = 'shipped'
),
pkgs as (
  select count(*) as labels
  from kpi_shipments
  where ship_date between p_start and p_end
    and not voided
    and queue in ('Warehouse', 'Freight')
),
ext as (
  select count(*) as n from o_out o
  where not exists (select 1 from kpi_shipments s where s.order_id = o.order_id and not s.voided)
),
sla as (
  select count(*) filter (where queue_bhours <= 48) as on_time,
         count(*) as total
  from o_out
  where queue = 'Warehouse' and queue_bhours is not null
),
qtime as (
  select avg(queue_bhours) as all_q,
         avg(queue_bhours) filter (where queue = 'Warehouse') as wh,
         avg(queue_bhours) filter (where queue = 'Freight')   as fr
  from o_out where queue_bhours is not null
),
builds as (
  select count(*) as n, coalesce(sum(quantity), 0) as units, count(distinct product_id) as skus
  from kpi_builds where complete_date between p_start and p_end
),
pos as (
  select count(*) as n, coalesce(sum(value), 0) as value
  from kpi_purchase_orders where order_date between p_start and p_end
),
rcpt as (
  select r.lead_days, o.origin
  from kpi_receipts r
  left join kpi_supplier_origin o on lower(o.supplier) = lower(trim(r.supplier))
  where r.receive_date between p_start and p_end and r.lead_days is not null
),
lead as (
  select avg(lead_days) as all_l,
         avg(lead_days) filter (where origin = 'domestic') as dom,
         avg(lead_days) filter (where origin = 'import')   as imp,
         count(*) as n,
         count(*) filter (where origin is null) as unclassified
  from rcpt
),
added as (
  select count(*) as n from kpi_products_added where week_of between p_start and p_end
),
acc as (
  select distinct on (location) location, count_week, accuracy
  from kpi_cycle_counts
  where count_week <= p_end and accuracy is not null
  order by location, count_week desc
),
inv as (
  select avg(total_value) as v, count(*) as d
  from kpi_inventory_daily where snap_date between p_start and p_end
),
sales as (
  select sum(dollars) as dl, count(*) as d
  from kpi_sales_daily where sale_date between p_start and p_end
),
queue_days as (
  select count(distinct snap_date) as n from kpi_queue_daily where snap_date between p_start and p_end
),
lines as (
  select 'received'::text as src, e->>'sku' as sku, coalesce((e->>'qty')::numeric, 0) as qty
  from o_in, jsonb_array_elements(coalesce(o_in.items, '[]'::jsonb)) e
  union all
  select 'shipped', e->>'sku', coalesce((e->>'qty')::numeric, 0)
  from o_out, jsonb_array_elements(coalesce(o_out.items, '[]'::jsonb)) e
  union all
  select 'queue', q.product_id, q.units / nullif((select n from queue_days), 0)
  from kpi_queue_daily q where q.snap_date between p_start and p_end
),
la as (
  select l.src, l.qty,
         coalesce(nullif(trim(a.category), ''), 'Not set') as category,
         coalesce(nullif(trim(a.chem), ''),     'Not set') as chem,
         coalesce(nullif(trim(a.lab_room), ''), 'Not set') as lab_room,
         coalesce(nullif(trim(a.downpack), ''), 'Not set') as downpack,
         coalesce(nullif(trim(a.hazmat), ''),   'Not set') as hazmat
  from lines l
  left join kpi_product_attrs a on a.product_id = l.sku
  where l.qty > 0
),
dims as (
  select src, 'category' as dim, category as val, sum(qty) as units from la group by 1, 2, 3
  union all select src, 'chem',     chem,     sum(qty) from la group by 1, 2, 3
  union all select src, 'lab_room', lab_room, sum(qty) from la group by 1, 2, 3
  union all select src, 'downpack', downpack, sum(qty) from la group by 1, 2, 3
  union all select src, 'hazmat',   hazmat,   sum(qty) from la group by 1, 2, 3
),
by_src as (
  select dim, src, jsonb_object_agg(val, round(units, 2)) as vals from dims group by dim, src
),
by_dim as (
  select dim, jsonb_object_agg(src, vals) as srcs from by_src group by dim
),
last_sync as (
  select max(run_at) as t from kpi_sync_log where ok
)
select jsonb_build_object(
  'start', p_start,
  'end', p_end,
  'last_sync', (select t from last_sync),
  'orders_since', (select min(order_date) from kpi_orders),
  'has', jsonb_build_object(
    'orders',   exists(select 1 from kpi_orders),
    'builds',   exists(select 1 from kpi_builds),
    'pos',      exists(select 1 from kpi_purchase_orders),
    'receipts', exists(select 1 from kpi_receipts),
    'staging',  exists(select 1 from kpi_products_added),
    'counts',   exists(select 1 from kpi_cycle_counts),
    'attrs',    exists(select 1 from kpi_product_attrs),
    'queue',    exists(select 1 from kpi_queue_daily)
  ),
  'orders_in',        (select count(*) from o_in),
  'orders_out',       (select count(*) from o_out),
  'warehouse_orders', (select count(*) from o_in where queue = 'Warehouse'),
  'freight_orders',   (select count(*) from o_in where queue = 'Freight'),
  'packages_shipped', (select labels from pkgs) + (select n from ext),
  'sla_48_pct',       (select case when total > 0 then on_time::numeric / total end from sla),
  'sla_48_sample',    (select total from sla),
  'queue_hours',      (select all_q from qtime),
  'queue_hours_wh',   (select wh from qtime),
  'queue_hours_fr',   (select fr from qtime),
  'builds_completed', (select n from builds),
  'units_built',      (select units from builds),
  'unique_skus_built',(select skus from builds),
  'pos_issued',       (select n from pos),
  'po_value',         (select value from pos),
  'lead_days',        (select all_l from lead),
  'lead_days_domestic',(select dom from lead),
  'lead_days_import', (select imp from lead),
  'lead_receipts',    (select n from lead),
  'lead_unclassified',(select unclassified from lead),
  'products_added',   (select n from added),
  'accuracy_avg',     (select avg(accuracy) from acc),
  'accuracy_by_location', (select coalesce(jsonb_agg(jsonb_build_object(
                              'location', location, 'week', count_week, 'accuracy', accuracy)
                              order by location), '[]'::jsonb) from acc),
  'turnover',         (select case when inv.v > 0 and sales.d > 0
                                   then sales.dl / inv.v * 365.0 / (select days from span) end
                       from inv, sales),
  'turnover_days_covered', (select d from inv),
  'queue_snapshot_days', (select n from queue_days),
  'mix', coalesce((select jsonb_object_agg(dim, srcs) from by_dim), '{}'::jsonb)
);
$$;

grant execute on function kpi_summary(date, date) to anon, authenticated;
grant select on kpi_inventory_daily, kpi_supplier_list to anon, authenticated;
