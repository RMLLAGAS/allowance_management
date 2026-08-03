# Hybrid Offline + Online Sync — Implementation Guide

## Files in this drop

| File | Status | Purpose |
|---|---|---|
| `database.py` | **modified copy** — additive only | sync columns, `sync_queue`/`sync_meta` tables, `*_active` views, `mark_created/updated/deleted()` helpers |
| `network.py` | new | cached internet connectivity check |
| `cloud.py` | new | Supabase (PostgREST) client — push/pull, credential handling |
| `sync.py` | new | the sync engine: push, pull, conflict resolution, retry queue, background loop, status |
| `api.py` | new | Flask Blueprint — `/sync/status`, `/sync/manual` |
| `supabase_schema.sql` | new | run once in the Supabase SQL editor |
| `MAIN_PY_INTEGRATION.md` | new | the exact ~21 line-level additions your `main.py` needs |

`main.py` itself is **not** included/rewritten — everything it needs is a small,
mechanical addition, listed precisely in `MAIN_PY_INTEGRATION.md`, so you can apply
it to your real file yourself (or hand that file + your `main.py` to Claude Code to
apply automatically).

## Step-by-step implementation plan

1. Create a Supabase project (free tier is enough to start). Copy the Project URL
   and the **service_role** key from Settings → API.
2. Run `supabase_schema.sql` in the Supabase SQL Editor.
3. Drop `network.py`, `cloud.py`, `sync.py`, `api.py` next to your `main.py`/`database.py`.
4. Replace your `database.py` with the modified copy here (diff it first if you've
   changed it since this upload — the sync additions are all appended near the
   bottom / inside `init_db()`, so a manual merge is straightforward).
5. Apply the 21 edits in `MAIN_PY_INTEGRATION.md`.
6. `pip install requests` (and `cryptography` if you want the optional encrypted
   local credential store instead of env vars).
7. Add `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` to `.env` (local) or your host's
   environment variables (Render, etc.).
8. Run the app. On first startup with credentials configured, the background
   scheduler fires an initial sync; every pre-existing local row gets backfilled
   with a `uuid` (done automatically inside `init_db()`) and pushed up.
9. Turn off your network and confirm the app still works exactly as before — that's
   the whole point: **sync is additive, offline is unchanged.**

## How each requirement is met

**Offline mode** — untouched. Every route still reads/writes SQLite directly through
`get_db()`, exactly as today. `sync.py` is only ever invoked by `trigger_async_sync()`
(login/logout, fire-and-forget in a background thread) or the 5-minute loop — never
inline in a request path, so a slow/dead network can never make a page hang.

**Online mode / automatic sync** — `sync.start_background_scheduler()` runs
`full_sync()` on startup and every 5 minutes; `trigger_async_sync()` runs it on login
and logout too. The manual "Sync Now" badge hits `POST /sync/manual`.

**Conflict handling** — `version` (monotonic per-record counter) is the primary
ordering signal, `updated_at` breaks ties; see the block comment at the top of
`sync.py`. A soft delete is just a write like any other, so the same rule decides
whether a delete or a later edit wins.

**Never duplicate records** — every table's cloud primary key is `uuid`
(`unique`), and pushes use `on_conflict=uuid` (Postgres `UPSERT`), so re-pushing the
same record is always idempotent. Locally, `idx_<table>_uuid` is a `UNIQUE INDEX`.

**Soft deletes** — `mark_deleted()` sets `deleted_at`/bumps `version` instead of
`DELETE`ing. The `transactions_active` / `savings_goals_active` views make every
existing read query ignore soft-deleted rows automatically — same UI behavior, data
never destroyed.

**Internet detection** — `network.is_online()`, a cached raw-socket probe against
Google/Cloudflare DNS (fast, no dependency on Supabase being up specifically).

**Background sync** — daemon thread started once at import time; also fired on
login/logout as one-off background threads so they never block those requests.

**Sync status UI** — `GET /sync/status` returns one of 🟢/🔴/🔄/✅/⚠ plus a pending
count; `MAIN_PY_INTEGRATION.md` §19 wires this into the existing top bar with a
20-second poll and a tap-to-sync button — no new template needed.

**Backup before sync** — `sync.full_sync()` calls `database.backup_db()` (your
existing, already-safe `shutil.copy2` snapshot function) as its first step, every
time, before touching anything.

**Security** — credentials come from environment variables only (`SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`); Supabase's REST API is HTTPS-only by construction; RLS is
enabled with no anon/authenticated policies, so only the server-side `service_role`
key can reach the tables at all (see the note in `supabase_schema.sql`). An optional
`cryptography`-based encrypted-at-rest fallback is included in `cloud.py` for hosts
where env vars aren't convenient. `password_hash` is deliberately **not** synced —
see the note at the bottom of `supabase_schema.sql`.

**Error handling** — every `cloud.py` call raises a single `CloudError` on any
network/HTTP failure; `sync.py` catches it, requeues the affected records with
linear backoff (`RETRY_BACKOFF_SECONDS * attempts`, capped at `MAX_ATTEMPTS=8`), and
reports `sync_state="failed"` without ever raising out of a request handler.
Duplicate records are structurally prevented (see above), not just retried around.
A partial push/pull (some tables succeed, one fails) is safe because each table is
processed and committed independently inside the same `full_sync()` call.

**Performance** — pushes only ever send rows sitting in `sync_queue` (changed since
last sync); pulls filter server-side with `updated_at=gt.<since>` so only new
remote changes are downloaded. The full table is never transferred in either
direction.

**Project structure** — only `network.py`, `cloud.py`, `sync.py`, `api.py` are new
top-level modules, exactly as requested; every other file is either untouched or
additively patched.

## What's intentionally out of scope here (call these out if you want them added)

- Real per-end-user Supabase Auth (currently the Flask server is the sole client,
  using one shared `service_role` key — fine for a single-deployment app, but if you
  ever expose Supabase directly to a mobile client you'd want per-user JWTs + RLS
  policies keyed on `auth.uid()` instead of the current server-only lockdown).
- Syncing receipt/profile-photo *binary bytes* to Supabase Storage — right now those
  stay device-local (only the DB row referencing them syncs). Straightforward to add
  as a `storage.py` module following the same push/pull pattern if you want images
  to follow the user across devices too.
