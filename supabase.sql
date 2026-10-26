-- Run once in Supabase → SQL Editor. Safe to run again.
-- The tables store.py uses. Row Level Security is on with no policies, so the
-- public (anon) key can't read or write them; only the app's secret key can.

create table if not exists app_kv (
  key text primary key,
  value jsonb not null,
  version bigint not null default 1,
  updated_at timestamptz not null default now()
);

create table if not exists app_changes (
  id bigserial primary key,
  app text not null,
  entry jsonb not null,
  at timestamptz not null default now()
);

create table if not exists app_locks (
  name text primary key,
  token text not null,
  expires_at timestamptz not null
);

alter table app_kv enable row level security;
alter table app_changes enable row level security;
alter table app_locks enable row level security;
