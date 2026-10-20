-- SoulSync AI — FULL schema for an EMPTY Supabase project (includes HEAVEN features).
-- Safe to re-run (IF NOT EXISTS). Run once in: Supabase Dashboard → SQL Editor → New query → paste → Run.
-- Reconstructed from app.py (no original schema file existed). Flask uses the service_role key, which bypasses RLS.

create extension if not exists pgcrypto;

-- ───────── Core ─────────
create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  first_name text not null, last_name text not null,
  email text not null unique, phone text, password_hash text not null,
  dob date, age int,
  gender text default 'other', interested_in text default 'everyone',
  onboarded boolean default false,
  is_verified boolean default false,
  verification_status text default 'unverified' check (verification_status in ('unverified','pending','verified','rejected')),
  avatar text, screening_complete boolean default false, voice_verified boolean default false,
  created_at timestamptz default now()
);

create table if not exists profiles (
  id uuid default gen_random_uuid(),
  user_id uuid primary key references users(id) on delete cascade,
  bio text default '', city text default '', occupation text default '',
  photos jsonb default '[]'::jsonb, anonymous_mode boolean default false,
  updated_at timestamptz default now()
);
create table if not exists preferences (
  user_id uuid primary key references users(id) on delete cascade,
  age_min int default 22, age_max int default 35, looking_for text, max_distance int default 50, education text,
  updated_at timestamptz default now()
);
create table if not exists personality_profiles (
  user_id uuid primary key references users(id) on delete cascade,
  energy_type text, conflict_style text, communication_type text, goal_alignment int, wants_children text,
  openness int, conscientiousness int, extraversion int, agreeableness int, emotional_stability int,
  updated_at timestamptz default now()
);
create table if not exists ai_twins (
  user_id uuid primary key references users(id) on delete cascade,
  description text, traits jsonb, created_at timestamptz default now()
);

-- ───────── Matching & chat ─────────
create table if not exists likes (
  id uuid primary key default gen_random_uuid(),
  liker_id uuid not null references users(id) on delete cascade,
  liked_id uuid not null references users(id) on delete cascade,
  created_at timestamptz default now(),
  unique (liker_id, liked_id)
);
create table if not exists matches (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  phase text default 'anon', matched_at timestamptz default now(),
  status text default 'active', ended_at timestamptz, ended_by uuid,
  unique (user1_id, user2_id)
);
create table if not exists conversations (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  phase text default 'anon', created_at timestamptz default now(), updated_at timestamptz default now(),
  unique (user1_id, user2_id)
);
create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  sender_id uuid not null references users(id) on delete cascade,
  content text not null, type text default 'text', is_read boolean default false,
  created_at timestamptz default now()
);
create index if not exists messages_convo_idx on messages(conversation_id, created_at);
create table if not exists compatibility_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  match_id uuid not null, score int, created_at timestamptz default now(),
  unique (user_id, match_id)
);
create table if not exists notifications (
  id uuid primary key default gen_random_uuid(),
  recipient_id uuid not null references users(id) on delete cascade,
  actor_id uuid, type text, title text, body text,
  is_read boolean default false, created_at timestamptz default now()
);
create index if not exists notifications_recipient_idx on notifications(recipient_id, is_read);

-- ───────── Reveals, missions, video, feedback ─────────
create table if not exists reveal_requests (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  requester_id uuid not null, current_phase text not null,
  status text default 'pending', created_at timestamptz default now(),
  unique (conversation_id, requester_id, current_phase)
);
create table if not exists photo_reveal_requests (
  id uuid primary key default gen_random_uuid(),
  sender_id uuid not null references users(id) on delete cascade,
  receiver_id uuid not null references users(id) on delete cascade,
  status text default 'pending', created_at timestamptz default now(), accepted_at timestamptz
);
create table if not exists photo_reveal_permissions (
  user1_id uuid not null, user2_id uuid not null, enabled boolean default true,
  primary key (user1_id, user2_id)
);
create table if not exists missions (
  id uuid primary key default gen_random_uuid(),
  name text not null, description text default '', emoji text default '🎯', category text default 'General',
  creator_id uuid references users(id) on delete set null, created_at timestamptz default now()
);
create table if not exists mission_members (
  mission_id uuid not null references missions(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  joined_at timestamptz default now(),
  primary key (mission_id, user_id)
);
create table if not exists video_dates (
  id uuid primary key default gen_random_uuid(),
  host_id uuid not null references users(id) on delete cascade,
  guest_id uuid not null references users(id) on delete cascade,
  conversation_id uuid, scheduled_at timestamptz,
  status text default 'invited' check (status in ('invited','scheduled','accepted','rejected','cancelled','completed')),
  room_id text unique, created_at timestamptz default now()
);
create table if not exists feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  match_id text, rating int default 3, comment text default '', type text default 'match_quality',
  created_at timestamptz default now()
);

-- ───────── HEAVEN features ─────────
create table if not exists user_streaks (
  user_id uuid primary key references users(id) on delete cascade,
  current_streak int default 0, longest_streak int default 0, last_active_date date, updated_at timestamptz default now()
);
create table if not exists verification_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  kind text check (kind in ('photo','voice')),
  status text default 'pending' check (status in ('pending','verified','rejected')),
  storage_path text, challenge text, submitted_at timestamptz default now(),
  reviewed_by uuid, reviewed_at timestamptz, reject_reason text
);
create table if not exists voice_prompts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  nonce text unique not null, phrase text, expires_at timestamptz, used boolean default false,
  created_at timestamptz default now()
);
create table if not exists ideal_partner_prefs (
  user_id uuid primary key references users(id) on delete cascade,
  interests text[], "values" text[], traits text[], goals text[], expectations text, ai_description text,
  updated_at timestamptz default now()
);
create table if not exists dating_requests (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,
  user2_id uuid not null references users(id) on delete cascade,
  sender_id uuid not null,
  status text default 'pending' check (status in ('pending','accepted','rejected','cancelled')),
  created_at timestamptz default now(), updated_at timestamptz
);
create unique index if not exists one_open_dating_request on dating_requests(user1_id, user2_id) where status in ('pending','accepted');
create table if not exists breakup_feedback (
  id uuid primary key default gen_random_uuid(),
  author_id uuid not null references users(id) on delete cascade,
  about_user_id uuid not null, reason text, comment text, created_at timestamptz default now()
);
create table if not exists contests (
  id uuid primary key default gen_random_uuid(),
  title text not null, rules text, starts_at timestamptz not null, ends_at timestamptz not null,
  status text default 'open' check (status in ('open','completed')), created_by uuid
);
create table if not exists contest_entries (
  id uuid primary key default gen_random_uuid(),
  contest_id uuid references contests(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  joined_at timestamptz default now(), rank int, score numeric,
  unique (contest_id, user_id)
);
create table if not exists contest_reveals (
  id uuid primary key default gen_random_uuid(),
  contest_id uuid references contests(id) on delete cascade,
  sender_id uuid not null, receiver_id uuid not null,
  status text default 'pending' check (status in ('pending','accepted','rejected')),
  created_at timestamptz default now(), responded_at timestamptz
);
create unique index if not exists one_open_contest_reveal on contest_reveals(contest_id, sender_id, receiver_id) where status in ('pending','accepted');

create table if not exists screening_profiles (
  user_id uuid primary key references users(id) on delete cascade,
  gender text, interested_in text, relationship_goal text, monogamy text, marriage_timeline text, children_plans text,
  comm_style text, emotional_readiness text, age_pref_min int check (age_pref_min between 18 and 99), age_pref_max int check (age_pref_max between 18 and 99),
  distance_pref int check (distance_pref between 5 and 1000), city text,
  lifestyle text[], "values" text[], interests text[], dealbreakers text[],
  consent jsonb, consent_at timestamptz, consent_version text, age_confirmed boolean default false,
  status text default 'incomplete' check (status in ('incomplete','complete')), updated_at timestamptz default now()
);

-- ───────── Security: block direct browser access (Flask uses the service_role key) ─────────
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- ───────── Private storage bucket for verification selfies/voice ─────────
insert into storage.buckets (id, name, public) values ('verification', 'verification', false) on conflict (id) do nothing;
