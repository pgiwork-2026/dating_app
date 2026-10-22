-- Block / report. Additive, safe to re-run.
create table if not exists blocks (blocker_id uuid not null, blocked_id uuid not null, created_at timestamptz default now(), primary key (blocker_id, blocked_id));
create table if not exists reports (
  id uuid primary key default gen_random_uuid(), reporter_id uuid not null, reported_id uuid not null,
  reason text default 'other', details text default '', status text default 'open' check (status in ('open','resolved')),
  created_at timestamptz default now(), resolved_by uuid, resolved_at timestamptz);
create index if not exists reports_open_idx on reports(status, created_at desc);
alter table blocks enable row level security; alter table reports enable row level security;