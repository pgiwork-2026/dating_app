-- Audit fixes - ADDITIVE ONLY, safe to re-run. Run in Supabase SQL Editor. No table is dropped and no row is changed or deleted.

-- 1) app.py reads and writes users.voice_verified (discovery, mutual matches, voice verification) but no earlier SQL file created it.
--    Without it, /api/matches fails with "column users.voice_verified does not exist" and voice verification can never be saved.
alter table users add column if not exists voice_verified boolean default false;
alter table users add column if not exists is_verified boolean default false;

-- 2) Row-level security on the core tables. Flask talks to Supabase with the service_role key, which BYPASSES RLS, so the app is
--    unaffected; the browser never calls Supabase directly. With RLS on and no policies, the public anon/authenticated keys get no access
--    to private data (messages, notifications, requests, likes...). Tables that do not exist are skipped.
do $$ declare t text; begin
  foreach t in array array['users','profiles','preferences','personality_profiles','ai_twins','likes','matches','conversations','messages',
                           'compatibility_reports','notifications','reveal_requests','photo_reveal_requests','photo_reveal_permissions',
                           'missions','mission_members','video_dates','feedback']
  loop
    if to_regclass('public.' || t) is not null then execute format('alter table %I enable row level security', t); end if;
  end loop;
end $$;

-- 3) Cheap lookups used by the request / notification flows.
create index if not exists notifications_actor_idx on notifications(recipient_id, actor_id, type) where is_read = false;
create index if not exists video_dates_pair_idx on video_dates(host_id, guest_id, status);
