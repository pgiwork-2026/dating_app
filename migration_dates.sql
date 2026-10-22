-- Real-life dates (both confirm; feedback private per person). Additive, safe to re-run.
create table if not exists real_dates (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  proposer_id uuid not null, date_on date not null, kind text default 'other',
  status text default 'pending' check (status in ('pending','confirmed','denied')),
  fb1 text check (fb1 in ('great','not_sure','uncomfortable')),
  fb2 text check (fb2 in ('great','not_sure','uncomfortable')),
  created_at timestamptz default now()
);
create index if not exists real_dates_pair_idx on real_dates(user1_id, user2_id, status);
alter table real_dates enable row level security;