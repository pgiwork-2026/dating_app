-- Row Level Security: ON for every app table, with NO policies = the public/anon key can read or write nothing.
-- The Flask server uses the service-role key, which bypasses RLS, so the app keeps working.
-- Run in the Supabase SQL editor AFTER migration_hardening.sql. Safe to re-run; missing tables are skipped.
do $$
declare t text;
begin
  foreach t in array array['active_pairings'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['ai_twins'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['blocks'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['breakup_feedback'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['challenge_profiles'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['challenge_submissions'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['closeness'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['compatibility_reports'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['contest_entries'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['contest_reveals'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['contests'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['conversations'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['couple_games'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['dating_requests'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['feedback'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['ideal_partner_prefs'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['likes'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['matches'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['messages'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['mission_members'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['missions'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['movie_logs'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['notifications'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['pairing_members'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['personality_profiles'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['photo_reveal_permissions'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['photo_reveal_requests'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['preferences'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['profiles'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['real_dates'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['reports'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['reveal_requests'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['revoked_tokens'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['screening_profiles'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['user_streaks'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['users'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['verification_requests'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['video_dates'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
  foreach t in array array['voice_prompts'] loop if to_regclass('public.'||t) is not null then execute format('alter table public.%I enable row level security', t); end if; end loop;
end $$;
