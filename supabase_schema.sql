-- =====================================================================================
--  supabase_schema.sql — cloud mirror of the SQLite schema, for Supabase (Postgres)
-- =====================================================================================
--  Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
--  Mirrors users / transactions / savings_goals from database.py plus the sync
--  columns (uuid, updated_at, deleted_at, sync_status, device_id, version).
--
--  IDENTITY: `uuid` is the shared primary key across every device + the cloud —
--  the local SQLite autoincrement `id` never leaves the device it was created on.
--
--  RLS: this app uses one Supabase project per deployment with a single shared
--  service_role key held server-side (never sent to a browser/client), and scopes
--  every row to `owner_user_id` (the *username*, since local SQLite ids aren't
--  portable across devices). Policies below restrict access to service_role only —
--  the Flask server is the only thing that ever talks to Supabase directly.
-- =====================================================================================

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- USERS  (profile/settings fields worth syncing — NOT password_hash, which
-- stays local-only per-device on purpose; see note below)
-- ---------------------------------------------------------------------------
create table if not exists users (
    uuid                   uuid primary key default gen_random_uuid(),
    owner_user_id          text not null,        -- local users.username, ties rows to an account
    full_name              text not null,
    username               text not null,
    profile_pic            text,
    cover_photo            text,
    email                  text,
    bio                    text,
    monthly_budget         numeric default 0,
    weekly_budget          numeric default 0,
    daily_budget           numeric default 0,
    yearly_budget          numeric default 0,
    theme                  text default 'blue',
    notifications_enabled  boolean default true,
    budget_alerts          boolean default true,
    savings_alerts         boolean default true,
    session_timeout        integer default 30,
    updated_at             timestamptz not null default now(),
    deleted_at             timestamptz,
    device_id              text,
    version                integer not null default 1
);
create unique index if not exists idx_users_uuid on users(uuid);
create index if not exists idx_users_owner on users(owner_user_id);

-- ---------------------------------------------------------------------------
-- TRANSACTIONS
-- ---------------------------------------------------------------------------
create table if not exists transactions (
    uuid          uuid primary key default gen_random_uuid(),
    owner_user_id text not null,
    type          text not null check (type in ('allowance','expense','savings')),
    amount        numeric not null check (amount > 0),
    category      text,
    date          text not null,
    notes         text,
    receipt       text,
    recurring     text default 'none',
    goal_id       uuid,                 -- references savings_goals.uuid (not local id)
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    deleted_at    timestamptz,
    device_id     text,
    version       integer not null default 1
);
create unique index if not exists idx_transactions_uuid on transactions(uuid);
create index if not exists idx_transactions_owner on transactions(owner_user_id);
create index if not exists idx_transactions_updated on transactions(updated_at);

-- ---------------------------------------------------------------------------
-- SAVINGS GOALS
-- ---------------------------------------------------------------------------
create table if not exists savings_goals (
    uuid          uuid primary key default gen_random_uuid(),
    owner_user_id text not null,
    goal_name     text not null,
    goal_amount   numeric not null,
    current_saved numeric default 0,
    deadline      text,
    created_at    timestamptz not null default now(),
    status        text default 'active',
    updated_at    timestamptz not null default now(),
    deleted_at    timestamptz,
    device_id     text,
    version       integer not null default 1
);
create unique index if not exists idx_savings_goals_uuid on savings_goals(uuid);
create index if not exists idx_savings_goals_owner on savings_goals(owner_user_id);
create index if not exists idx_savings_goals_updated on savings_goals(updated_at);

-- ---------------------------------------------------------------------------
-- ROW LEVEL SECURITY — locked to service_role only. The Flask server (holding
-- SUPABASE_SERVICE_KEY, which bypasses RLS by design) is the sole caller;
-- browsers/mobile clients never talk to Supabase directly, so there is no
-- anon/authenticated policy to define here.
-- ---------------------------------------------------------------------------
alter table users enable row level security;
alter table transactions enable row level security;
alter table savings_goals enable row level security;
-- (No policies added = only service_role, which bypasses RLS, can access these
-- tables. This is intentional: it's the safest default until/unless you later
-- add real Supabase Auth sessions per end-user.)

-- ---------------------------------------------------------------------------
-- NOTE ON password_hash
-- ---------------------------------------------------------------------------
-- password_hash is deliberately excluded from the cloud `users` table. Syncing
-- credential hashes across every device multiplies the blast radius of any one
-- device/db being compromised for no real benefit (offline login only needs the
-- hash that's already local). If you want cross-device login (log in on a
-- brand-new device with no local account yet), sync it explicitly as a
-- conscious decision — add the column back and note the added risk.
