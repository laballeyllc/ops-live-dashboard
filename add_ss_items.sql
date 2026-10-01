-- Run this in Supabase: Project -> SQL Editor -> New query -> paste -> Run

alter table live_queue add column if not exists ss_items text;
alter table snapshots add column if not exists ss_items text;
