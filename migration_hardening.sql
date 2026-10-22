-- Hardening: server-side logout list. Additive, safe to re-run.
create table if not exists revoked_tokens (
  jti text primary key,
  user_id uuid,
  expires_at timestamptz not null,
  created_at timestamptz default now()
);
create index if not exists revoked_tokens_exp on revoked_tokens(expires_at);
-- optional housekeeping (run now and then): delete from revoked_tokens where expires_at < now();
