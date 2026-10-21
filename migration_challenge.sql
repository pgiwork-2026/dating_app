-- 21-day challenge, movie journal, exclusive pairing. Additive and safe to re-run. Nothing is dropped or rewritten.
alter table users add column if not exists challenge_complete boolean not null default false;

create table if not exists challenge_profiles (
  user_id uuid primary key references users(id) on delete cascade,
  hobby text, hobby_detail text,
  timezone text not null default 'Asia/Kolkata',
  tz_changed_at timestamptz,
  cutoff_hour int not null default 4 check (cutoff_hour between 0 and 6),   -- local day rolls over at this hour
  routine jsonb not null default '{}'::jsonb,        -- sleep/work schedule, habits, participation frequency
  genres_like text[] not null default '{}', genres_dislike text[] not null default '{}',
  languages text[] not null default '{}', fav_films text[] not null default '{}',
  onboarding_complete boolean not null default false,
  completed_at timestamptz, updated_at timestamptz default now()
);

create table if not exists challenge_submissions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  local_date date not null,                           -- computed by the server from the user's timezone, never sent by the client
  hobby text not null, file_path text not null, file_sha256 text not null,
  status text not null default 'pending' check (status in ('pending','approved','rejected')),
  reject_reason text, reviewed_by uuid, reviewed_at timestamptz,
  created_at timestamptz not null default now()
);
create unique index if not exists one_live_submission_per_day on challenge_submissions(user_id, local_date) where status in ('pending','approved');
create unique index if not exists no_replayed_proof on challenge_submissions(user_id, file_sha256);
create index if not exists challenge_sub_user on challenge_submissions(user_id, local_date);

create table if not exists movie_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  title text not null, title_key text not null, language text,
  genres text[] not null default '{}', rating int not null check (rating between 1 and 5),
  review text, watched_on date, proof_path text, created_at timestamptz not null default now(),
  unique (user_id, title_key)
);

-- One active exclusive pairing per user: pairing_members has one row per person, and a partial unique index allows only one active row each.
create table if not exists active_pairings (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  status text not null default 'active' check (status in ('active','ended')),
  created_at timestamptz not null default now(), ended_at timestamptz, ended_by uuid,
  check (user1_id < user2_id)
);
create table if not exists pairing_members (
  pairing_id uuid not null references active_pairings(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  active boolean not null default true,
  primary key (pairing_id, user_id)
);
create unique index if not exists one_active_pairing_per_user on pairing_members(user_id) where active;

create or replace function create_exclusive_pairing(a uuid, b uuid) returns uuid language plpgsql as $$
declare pid uuid; u1 uuid := least(a, b); u2 uuid := greatest(a, b);
begin
  if a = b then raise exception 'same_user'; end if;
  perform pg_advisory_xact_lock(hashtext(u1::text));      -- fixed lock order: no deadlocks, concurrent requests serialise
  perform pg_advisory_xact_lock(hashtext(u2::text));
  select id into pid from active_pairings where status = 'active' and user1_id = u1 and user2_id = u2;
  if pid is not null then return pid; end if;              -- same pair again: idempotent
  if exists (select 1 from pairing_members where active and user_id in (a, b)) then raise exception 'already_paired'; end if;
  insert into active_pairings(user1_id, user2_id) values (u1, u2) returning id into pid;
  insert into pairing_members(pairing_id, user_id) values (pid, u1), (pid, u2);
  return pid;
end $$;

create or replace function end_exclusive_pairing(a uuid, b uuid, who uuid) returns void language plpgsql as $$
begin
  update pairing_members set active = false where pairing_id in
    (select id from active_pairings where status = 'active' and user1_id = least(a, b) and user2_id = greatest(a, b));
  update active_pairings set status = 'ended', ended_at = now(), ended_by = who
    where status = 'active' and user1_id = least(a, b) and user2_id = greatest(a, b);
end $$;

revoke all on function create_exclusive_pairing(uuid, uuid) from public, anon, authenticated;
revoke all on function end_exclusive_pairing(uuid, uuid, uuid) from public, anon, authenticated;
alter table challenge_profiles enable row level security;
alter table challenge_submissions enable row level security;
alter table movie_logs enable row level security;
alter table active_pairings enable row level security;
alter table pairing_members enable row level security;
-- Private storage: proofs use the existing private VERIFY_BUCKET under challenge/<user_id>/ . No public bucket or URL is used.
