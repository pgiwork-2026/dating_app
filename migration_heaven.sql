-- HEAVEN upgrade — additive only. Safe to re-run. Run in Supabase SQL editor.
alter table users add column if not exists avatar text;
alter table users add column if not exists verification_status text default 'unverified';
alter table matches add column if not exists status text default 'active';
alter table matches add column if not exists ended_at timestamptz;
alter table matches add column if not exists ended_by uuid;
alter table video_dates add column if not exists status text default 'invited';

create table if not exists user_streaks (user_id uuid primary key, current_streak int default 0, longest_streak int default 0, last_active_date date, updated_at timestamptz default now());
create table if not exists verification_requests (id uuid primary key default gen_random_uuid(), user_id uuid not null, kind text check (kind in ('photo','voice')), status text default 'pending' check (status in ('pending','verified','rejected')), storage_path text, challenge text, submitted_at timestamptz default now(), reviewed_by uuid, reviewed_at timestamptz, reject_reason text);
create table if not exists voice_prompts (id uuid primary key default gen_random_uuid(), user_id uuid not null, nonce text unique not null, phrase text, expires_at timestamptz, used boolean default false, created_at timestamptz default now());
create table if not exists ideal_partner_prefs (user_id uuid primary key, interests text[], "values" text[], traits text[], goals text[], expectations text, ai_description text, updated_at timestamptz default now());
create table if not exists dating_requests (id uuid primary key default gen_random_uuid(), user1_id uuid not null, user2_id uuid not null, sender_id uuid not null, status text default 'pending' check (status in ('pending','accepted','rejected','cancelled')), created_at timestamptz default now(), updated_at timestamptz);
create unique index if not exists one_open_dating_request on dating_requests(user1_id, user2_id) where status in ('pending','accepted');
create table if not exists breakup_feedback (id uuid primary key default gen_random_uuid(), author_id uuid not null, about_user_id uuid not null, reason text, comment text, created_at timestamptz default now());
create table if not exists contests (id uuid primary key default gen_random_uuid(), title text not null, rules text, starts_at timestamptz not null, ends_at timestamptz not null, status text default 'open' check (status in ('open','completed')), created_by uuid);
create table if not exists contest_entries (id uuid primary key default gen_random_uuid(), contest_id uuid references contests(id) on delete cascade, user_id uuid not null, joined_at timestamptz default now(), rank int, score numeric, unique (contest_id, user_id));
create table if not exists contest_reveals (id uuid primary key default gen_random_uuid(), contest_id uuid references contests(id), sender_id uuid not null, receiver_id uuid not null, status text default 'pending' check (status in ('pending','accepted','rejected')), created_at timestamptz default now(), responded_at timestamptz);
create unique index if not exists one_open_contest_reveal on contest_reveals(contest_id, sender_id, receiver_id) where status in ('pending','accepted');

-- Block direct browser (anon/authenticated) access; Flask uses the service key.
do $$ declare t text; begin
  foreach t in array array['user_streaks','verification_requests','voice_prompts','ideal_partner_prefs','dating_requests','breakup_feedback','contests','contest_entries','contest_reveals']
  loop execute format('alter table %I enable row level security', t); end loop; end $$;

-- Existing users were all auto-marked verified by the old signup bug. Reset them (remove this line to keep them):
update users set is_verified = false where coalesce(verification_status,'unverified') <> 'verified';

-- Then in Dashboard > Storage: create a PRIVATE bucket named "verification".
