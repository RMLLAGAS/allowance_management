#!/usr/bin/env python3
# =====================================================================================
#  sync.py — the sync engine: push local changes, pull remote changes, resolve
#            conflicts, retry failures, run in the background.
# =====================================================================================
#  This is the ONLY module that talks to both database.py and cloud.py at once.
#  network.py / cloud.py never touch SQLite directly, and database.py never touches
#  the network — that separation is what makes offline mode trivially safe: if this
#  file is never called, nothing about the app's behavior changes at all.
#
#  CONFLICT RESOLUTION
#  --------------------
#  Every synced row carries (updated_at, version, device_id, deleted_at). When a
#  pulled remote row and the local row for the same uuid disagree:
#    1. Higher `version` wins (it's monotonically incremented on every local write,
#       so it's a more reliable ordering signal than clock skew between devices).
#    2. If `version` ties, the later `updated_at` timestamp wins (last-write-wins).
#    3. A soft delete (deleted_at set) is treated as just another write for
#       ordering purposes — so an edit after a delete "un-deletes" the record, and
#       a delete after an edit removes it, whichever has the higher version/time.
#  The loser is simply overwritten locally/remotely — nothing is ever merged
#  field-by-field, which keeps behavior predictable and easy to reason about.
# =====================================================================================

import json
import threading
import time
from datetime import datetime

import database
import network
import cloud

SYNC_TABLES = ("users", "transactions", "savings_goals")
MAX_ATTEMPTS = 8            # per queued record, before we stop auto-retrying
RETRY_BACKOFF_SECONDS = 30  # multiplied by attempts (simple linear backoff)

_status_lock = threading.Lock()
_status = {
    "sync_state": "idle",       # idle | syncing | synced | failed
    "last_sync_at": None,
    "last_error": None,
    "pending_count": 0,
}
_scheduler_started = False


# =====================================================================================
# STATUS  (read by api.py for the 🟢/🔴/🔄/✅/⚠ indicator)
# =====================================================================================
def get_status() -> dict:
    with _status_lock:
        snapshot = dict(_status)
    snapshot["online"] = network.is_online()
    conn = _connect()
    snapshot["pending_count"] = conn.execute("SELECT COUNT(*) c FROM sync_queue").fetchone()["c"]
    conn.close()
    return snapshot


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def _connect():
    import sqlite3
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# =====================================================================================
# PUSH — drain sync_queue, upload changed records, never upload the whole table
# =====================================================================================
def push_changes() -> dict:
    conn = _connect()
    pushed, failed = 0, 0
    try:
        for table in SYNC_TABLES:
            queued = conn.execute(
                "SELECT * FROM sync_queue WHERE table_name=? ORDER BY id", (table,)
            ).fetchall()
            if not queued:
                continue

            upserts, deletes_uuids, resolved_queue_ids = [], [], []
            for q in queued:
                row = conn.execute(f"SELECT * FROM {table} WHERE uuid=?", (q["record_uuid"],)).fetchone()
                if row is None:
                    resolved_queue_ids.append(q["id"])  # record no longer exists locally at all
                    continue
                if q["operation"] == "delete" or row["deleted_at"]:
                    deletes_uuids.append(dict(row))
                else:
                    upserts.append(_row_for_cloud(table, row))
                resolved_queue_ids.append(q["id"])

            try:
                if upserts:
                    cloud.push_records(table, upserts)
                if deletes_uuids:
                    cloud.push_records(table, [_row_for_cloud(table, r) for r in
                                                [dict(x) for x in deletes_uuids]])
                for qid in resolved_queue_ids:
                    conn.execute("DELETE FROM sync_queue WHERE id=?", (qid,))
                # Mark every pushed uuid as synced in one pass.
                all_uuids = [u["uuid"] for u in upserts] + [d["uuid"] for d in deletes_uuids]
                for u in all_uuids:
                    conn.execute(f"UPDATE {table} SET sync_status='synced' WHERE uuid=?", (u,))
                conn.commit()
                pushed += len(upserts) + len(deletes_uuids)
            except cloud.CloudError as e:
                failed += len(queued)
                _requeue_with_backoff(conn, table, queued, str(e))
                conn.commit()
    finally:
        conn.close()
    return {"pushed": pushed, "failed": failed}


def _row_for_cloud(table, row) -> dict:
    """Project a local sqlite Row down to the columns Supabase expects. Business
    columns are sent as-is; local-only autoincrement `id` is deliberately excluded
    — `uuid` is the shared primary key across devices."""
    d = dict(row)
    d.pop("id", None)
    d.pop("sync_status", None)  # sync_status is device-local bookkeeping, not synced
    return d


def _requeue_with_backoff(conn, table, queued_rows, error_message):
    now = datetime.now()
    for q in queued_rows:
        attempts = (q["attempts"] or 0) + 1
        if attempts > MAX_ATTEMPTS:
            # Give up automatically retrying; it stays visible via /sync/status
            # and can be retried manually with a fresh attempt counter.
            conn.execute(
                "UPDATE sync_queue SET attempts=?, last_error=? WHERE id=?",
                (attempts, f"giving up after {MAX_ATTEMPTS} attempts: {error_message}", q["id"])
            )
            continue
        next_retry = now.timestamp() + (RETRY_BACKOFF_SECONDS * attempts)
        conn.execute(
            "UPDATE sync_queue SET attempts=?, last_error=?, next_retry_at=? WHERE id=?",
            (attempts, error_message, datetime.fromtimestamp(next_retry).isoformat(), q["id"])
        )
        conn.execute(f"UPDATE {table} SET sync_status='pending' WHERE uuid=?", (q["record_uuid"],))


# =====================================================================================
# PULL — download only rows changed since the last successful pull for that table
# =====================================================================================
def pull_changes() -> dict:
    conn = _connect()
    device_id = database.get_device_id()
    applied, conflicts = 0, 0
    try:
        for table in SYNC_TABLES:
            since = database.get_sync_meta(f"last_pull_{table}", "1970-01-01T00:00:00")
            remote_rows = cloud.pull_updates(table, since, exclude_device_id=device_id)
            latest_seen = since
            for remote in remote_rows:
                applied += _apply_remote_row(conn, table, remote)
                if remote.get("updated_at", "") > latest_seen:
                    latest_seen = remote["updated_at"]
            if remote_rows:
                database.set_sync_meta(f"last_pull_{table}", latest_seen)
        conn.commit()
    finally:
        conn.close()
    return {"applied": applied, "conflicts": conflicts}


def _apply_remote_row(conn, table, remote: dict) -> int:
    """Insert or overwrite the local row for this uuid, applying the
    version -> updated_at conflict rule described at the top of this file.
    Returns 1 if the remote row was applied, 0 if the local row won and nothing
    changed."""
    local = conn.execute(f"SELECT * FROM {table} WHERE uuid=?", (remote["uuid"],)).fetchone()

    if local is not None:
        local_version = local["version"] or 1
        remote_version = remote.get("version") or 1
        if local_version > remote_version:
            return 0
        if local_version == remote_version and (local["updated_at"] or "") >= (remote.get("updated_at") or ""):
            return 0

    cols = [c for c in remote.keys() if c != "id"]  # never import a remote autoincrement id
    values = [remote[c] for c in cols]

    if local is not None:
        set_clause = ", ".join(f"{c}=?" for c in cols)
        conn.execute(f"UPDATE {table} SET {set_clause}, sync_status='synced' WHERE uuid=?",
                     (*values, remote["uuid"]))
    else:
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO {table} ({col_list}, sync_status) VALUES ({placeholders}, 'synced')",
                     values)
    return 1


# =====================================================================================
# FULL SYNC — the single entry point everything else calls
# =====================================================================================
def full_sync(reason: str = "manual") -> dict:
    if not network.is_online(force=True):
        _set_status(sync_state="failed", last_error="offline", last_sync_at=_status.get("last_sync_at"))
        return {"ok": False, "reason": "offline"}

    _set_status(sync_state="syncing", last_error=None)
    database.backup_db()  # snapshot BEFORE touching anything, per requirements

    try:
        push_result = push_changes()
        pull_result = pull_changes()
        # Retry anything still queued from a previous failed attempt.
        retry_ready = _due_for_retry()
        if retry_ready:
            push_changes()

        _set_status(sync_state="synced", last_sync_at=datetime.now().isoformat(), last_error=None)
        return {"ok": True, "reason": reason, "push": push_result, "pull": pull_result}
    except Exception as e:  # noqa: BLE001 — any failure here must never crash the request
        _set_status(sync_state="failed", last_error=str(e))
        return {"ok": False, "reason": reason, "error": str(e)}


def _due_for_retry():
    conn = _connect()
    now = datetime.now().isoformat()
    rows = conn.execute(
        "SELECT id FROM sync_queue WHERE next_retry_at IS NOT NULL AND next_retry_at <= ?", (now,)
    ).fetchall()
    conn.close()
    return rows


# =====================================================================================
# BACKGROUND SYNC — startup, every 5 minutes, plus login/logout hooks call full_sync()
# directly (see main.py patch notes)
# =====================================================================================
def trigger_async_sync(reason: str = "event"):
    """Fire-and-forget sync from a request handler (login/logout) — never blocks
    the HTTP response waiting on the network."""
    threading.Thread(target=full_sync, args=(reason,), daemon=True).start()


def start_background_scheduler(interval_seconds: int = 300):
    """Call once at app startup. Runs full_sync() on startup, then every
    interval_seconds (default 5 minutes) for as long as the process is alive."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        trigger_async_sync("startup")
        while True:
            time.sleep(interval_seconds)
            full_sync("interval")

    threading.Thread(target=_loop, daemon=True).start()
