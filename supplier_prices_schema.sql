-- Supplier pricing: one row per received or completed purchase-order line, loaded daily
-- from the Finale report "SCM_Purchase order w/ detail" by pull_supplier_prices.py.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to re-run.

create table if not exists supplier_prices (
  line_key              text primary key,     -- stable id of the PO line (see pull_supplier_prices.py)
  po_id                 text        not null, -- Finale "Order ID"
  order_date            date        not null, -- the PO's order date: the time axis ("price at time of purchase")
  supplier              text        not null,
  product_id            text        not null,
  description           text,
  category              text,
  destination           text,                 -- Finale "Destination" (e.g. Dripping Springs, Dropship); the page filters on it
  order_status          text,
  quantity              numeric,              -- ordered
  quantity_received     numeric,              -- received (0 on a Completed dropship line, which is never received)
  unit_price            numeric     not null, -- the PO line's unit price
  landed_cost_per_unit  numeric,              -- Finale's estimate (unit price plus freight and fees); the page's "Landed cost" view
  eta                   date,                 -- the PO line's expected date, for on-time delivery
  received_date         date,                 -- when it arrived (latest receipt), from Finale's shipments summary
  terms                 text,                 -- e.g. Net 30 days
  fulfillment           text,                 -- e.g. Freight, Customer pickup
  updated_at            timestamptz default now()
);

-- If you ran an earlier copy of this file, this adds the column it lacked.
alter table supplier_prices add column if not exists destination text;
alter table supplier_prices add column if not exists eta date;
alter table supplier_prices add column if not exists received_date date;
alter table supplier_prices add column if not exists terms text;
alter table supplier_prices add column if not exists fulfillment text;

create index if not exists supplier_prices_product_date_idx  on supplier_prices (product_id, order_date);
create index if not exists supplier_prices_supplier_date_idx on supplier_prices (supplier, order_date);
create index if not exists supplier_prices_date_idx          on supplier_prices (order_date);

-- Same access pattern as the KPI tables: the page reads with the public
-- (anon) key; only the pull script, using the service key, writes.
alter table supplier_prices enable row level security;
do $$
begin
  if not exists (select 1 from pg_policies where tablename = 'supplier_prices' and policyname = 'public read') then
    create policy "public read" on supplier_prices for select using (true);
  end if;
end $$;
grant select on supplier_prices to anon, authenticated;

-- Last-12-months sales per product, for the page's margin figures (what a resold SKU sells for vs what it costs).
-- Built from the turnover job's product_sales_daily; skipped quietly if that table doesn't exist yet.
do $$
begin
  if to_regclass('public.product_sales_daily') is not null then
    execute $v$
      create or replace view supplier_sales_12m with (security_invoker = true) as
      select product_id,
             sum(units_sold)   as units_12m,
             sum(dollars_sold) as dollars_12m
      from product_sales_daily
      where sale_date >= current_date - 365 and units_sold > 0
      group by product_id
    $v$;
    execute 'grant select on supplier_sales_12m to anon, authenticated';
  end if;
end $$;
