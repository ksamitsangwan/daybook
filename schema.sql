-- Daybook database for Supabase (PostgreSQL). Run once:
-- Supabase dashboard -> SQL Editor -> New query -> paste all of this -> Run.
--
-- Everything Daybook stores (tasks, tours, habits, days, saved step lists...)
-- lives in ONE table as flexible JSON documents. New features never need
-- database changes: the app just saves new fields inside "data".

create table if not exists public.docs (
  user_id     uuid        not null default auth.uid() references auth.users(id) on delete cascade,
  col         text        not null,   -- which kind: tasks, tours, habits, days, ...
  id          text        not null,   -- the item's id
  data        jsonb       not null default '{}'::jsonb,
  updated_at  timestamptz not null default now(),
  primary key (user_id, col, id)
);

create index if not exists docs_user_col on public.docs (user_id, col);

-- Security: a signed-in person can only ever see and change their own rows.
alter table public.docs enable row level security;
drop policy if exists "own docs" on public.docs;
create policy "own docs" on public.docs
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- Live sync between your iPhone and Mac.
alter table public.docs replica identity full;
do $$
begin
  begin alter publication supabase_realtime add table public.docs;
  exception when duplicate_object then null; end;
end $$;
