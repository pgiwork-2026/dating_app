-- Couple Zone private game rooms (Tic-Tac-Toe, Truth or Dare, How Well Do You Know Me, Memory Match, Couple Puzzle).
-- Additive and safe to re-run. Run once in the Supabase SQL editor.
create table if not exists couple_games (
  id uuid primary key default gen_random_uuid(),
  user1_id uuid not null references users(id) on delete cascade,   -- the pair, stored sorted (min, max) like matches
  user2_id uuid not null references users(id) on delete cascade,
  inviter_id uuid not null references users(id) on delete cascade,
  game text not null check (game in ('tictactoe','truthdare','knowme','memory','puzzle')),
  status text not null default 'invited' check (status in ('invited','active','finished','declined','cancelled')),
  state jsonb not null default '{}'::jsonb,
  version int not null default 1,                                    -- optimistic lock + cheap change detection for polling
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create unique index if not exists one_open_couple_game on couple_games (user1_id, user2_id) where status in ('invited','active');
create index if not exists couple_games_pair_idx on couple_games (user1_id, user2_id, created_at desc);
alter table couple_games enable row level security;   -- the Flask service key bypasses RLS; no public policies on purpose
