-- Fresh start: wipes likes/matches/chats/etc. but KEEPS users, profiles,
-- preferences, personality_profiles, ai_twins, screening_profiles.
-- Run in Supabase SQL Editor. Missing tables are skipped safely.
do $$
declare t text;
begin
  foreach t in array array[
    'messages','reveal_requests','conversations',
    'likes','matches','compatibility_reports','closeness',
    'dating_requests','breakup_feedback',
    'photo_reveal_requests','photo_reveal_permissions',
    'video_dates','notifications','user_streaks',
    'pairing_members','active_pairings','movie_logs'
  ] loop
    if to_regclass('public.'||t) is not null then
      execute format('truncate table public.%I restart identity cascade', t);
      raise notice 'cleared %', t;
    end if;
  end loop;
end $$;