-- Closeness graph + engagement flow. Additive and safe to re-run.
create table if not exists closeness (
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  pct int default 0,
  score_offset int default 0,
  status text default 'building' check (status in ('building','asking','engaged')),
  answer1 text check (answer1 in ('yes','more_time','break_up')),
  answer2 text check (answer2 in ('yes','more_time','break_up')),
  history jsonb default '[]'::jsonb,
  asked_at timestamptz, updated_at timestamptz default now(),
  primary key (user1_id, user2_id)
);
alter table closeness enable row level security;