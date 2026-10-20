-- Profile screening — additive only; safe to re-run. Run in Supabase SQL Editor. No data is dropped or overwritten.
alter table users add column if not exists screening_complete boolean default false;
create table if not exists screening_profiles (
  user_id uuid primary key references users(id) on delete cascade,
  gender text, interested_in text, relationship_goal text, monogamy text, marriage_timeline text, children_plans text,
  comm_style text, emotional_readiness text, age_pref_min int check (age_pref_min between 18 and 99), age_pref_max int check (age_pref_max between 18 and 99),
  distance_pref int check (distance_pref between 5 and 1000), city text,
  lifestyle text[], "values" text[], interests text[], dealbreakers text[],
  consent jsonb, consent_at timestamptz, consent_version text, age_confirmed boolean default false,
  status text default 'incomplete' check (status in ('incomplete','complete')), updated_at timestamptz default now()
);
alter table screening_profiles enable row level security;   -- Flask uses the service_role key; browsers get no direct access
