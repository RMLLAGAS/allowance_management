#!/usr/bin/env python3
# =====================================================================================
#  database.py — Persistent data layer for Allowance Management
# =====================================================================================
#  This module owns EVERYTHING related to on-disk persistence: the SQLite database
#  file itself, its schema/migrations, and the folder layout for anything that must
#  survive a redeploy or update of main.py (receipts, profile photos, backups, and
#  exports).
#
#  DESIGN RULES — do not break these when editing this file:
#    1. main.py can be replaced/updated at any time without this module ever
#       deleting, truncating, or overwriting existing data.
#    2. The database file is only ever opened with sqlite3.connect(), which creates
#       it on first run and otherwise connects to whatever is already there. Nothing
#       in this file ever removes or recreates allowance.db.
#    3. Schema changes are additive ONLY: CREATE TABLE IF NOT EXISTS and
#       ALTER TABLE ... ADD COLUMN (guarded by a "does this column already exist"
#       check). Nothing here ever DROPs a table or DELETEs rows.
#    4. Automatic backups are copies (shutil.copy2) of the live database — the
#       original file is never moved or altered by the backup process.
# =====================================================================================

import os
import shutil
import sqlite3
from datetime import datetime

from flask import g

# =====================================================================================
# FOLDER LAYOUT
# =====================================================================================
#   data/
#     allowance.db     <- the SQLite database (never deleted/recreated)
#     receipts/        <- permanent on-disk copies of uploaded receipt images
#     profile/          <- permanent on-disk copies of profile pictures / cover photos
#     backups/           <- timestamped allowance.db snapshots, taken automatically
#   exports/
#     pdf/                <- saved copies of generated PDF/print exports
#     excel/              <- saved copies of generated CSV/Excel exports
#
# All paths can be overridden with environment variables (useful on hosts like Render
# where you'll want these pointed at a mounted persistent disk), but default to living
# right next to this file so a plain `python main.py` works out of the box.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR     = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
RECEIPTS_DIR = os.path.join(DATA_DIR, "receipts")
PROFILE_DIR  = os.path.join(DATA_DIR, "profile")
BACKUPS_DIR  = os.path.join(DATA_DIR, "backups")

EXPORTS_DIR       = os.environ.get("EXPORTS_DIR", os.path.join(BASE_DIR, "exports"))
EXPORTS_PDF_DIR   = os.path.join(EXPORTS_DIR, "pdf")
EXPORTS_EXCEL_DIR = os.path.join(EXPORTS_DIR, "excel")

# The database now lives inside data/ by default. DB_PATH can still be overridden
# directly (e.g. DB_PATH=/var/data/allowance.db on Render), which takes priority
# over the data/ default below.
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "allowance.db"))

# Where allowance.db used to live before this data/ restructure (directly next to
# main.py). Only used once, by _migrate_legacy_db(), to move an existing database
# into its new home instead of silently starting from an empty one.
_LEGACY_DB_PATH = os.path.join(BASE_DIR, "allowance.db")

MAX_DB_BACKUPS = 30  # automatic backups to keep in data/backups/ (oldest are pruned)


def ensure_folders():
    """Create every folder this app relies on, if it doesn't already exist.
    Safe to call on every startup: os.makedirs(..., exist_ok=True) never touches
    a folder — or anything inside it — that's already there."""
    for path in (DATA_DIR, RECEIPTS_DIR, PROFILE_DIR, BACKUPS_DIR,
                 EXPORTS_PDF_DIR, EXPORTS_EXCEL_DIR):
        os.makedirs(path, exist_ok=True)


def _migrate_legacy_db():
    """One-time migration for existing installs: earlier versions of this app kept
    allowance.db sitting directly next to main.py. If that file still exists and
    nothing has been created yet at the new data/allowance.db location, MOVE it
    into place rather than starting from an empty database. After the first run
    following an update, this is permanently a no-op (the legacy file is gone and
    the new path already has your data)."""
    if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        shutil.move(_LEGACY_DB_PATH, DB_PATH)
        print(f"[database] Migrated existing database: {_LEGACY_DB_PATH} -> {DB_PATH}")


# =====================================================================================
# REQUEST-SCOPED CONNECTION
# =====================================================================================
def get_db():
    """Return a request-scoped SQLite connection (one per request, cached on flask.g)."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exception=None):
    """Register with app.teardown_appcontext(...) — closes the per-request connection."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# =====================================================================================
# SCHEMA / MIGRATIONS — additive only. Never DROP a table, never DELETE a row.
# =====================================================================================
def init_db():
    """Connect to the existing allowance.db if one is present (migrating it from the
    legacy location first, if needed); otherwise create a fresh one. Only ever
    CREATEs missing tables or ALTERs in missing columns — existing tables, columns,
    and rows are never touched. Safe to call on every startup/redeploy."""
    ensure_folders()
    _migrate_legacy_db()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name             TEXT NOT NULL,
            username              TEXT NOT NULL UNIQUE,
            password_hash         TEXT NOT NULL,
            profile_pic           TEXT,
            cover_photo           TEXT,
            email                 TEXT UNIQUE,
            email_verified        INTEGER DEFAULT 0,
            verification_code     TEXT,
            verification_expiry   TEXT,
            date_joined           TEXT NOT NULL,
            last_login             TEXT,
            monthly_budget        REAL DEFAULT 0,
            weekly_budget         REAL DEFAULT 0,
            daily_budget          REAL DEFAULT 0,
            yearly_budget         REAL DEFAULT 0,
            theme                 TEXT DEFAULT 'blue',
            notifications_enabled INTEGER DEFAULT 1,
            budget_alerts         INTEGER DEFAULT 1,
            savings_alerts        INTEGER DEFAULT 1,
            session_timeout       INTEGER DEFAULT 30
        )
    """)

    # --- Safe migration: add columns to existing installs that predate them ---
    # (All existing columns and data are left completely untouched.)
    existing_user_cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "yearly_budget" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN yearly_budget REAL DEFAULT 0")
    if "cover_photo" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN cover_photo TEXT")
    if "email" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "email_verified" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
    if "verification_code" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN verification_code TEXT")
    if "verification_expiry" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN verification_expiry TEXT")
    if "bio" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN bio TEXT")

    c.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            goal_name     TEXT NOT NULL,
            goal_amount   REAL NOT NULL,
            current_saved REAL DEFAULT 0,
            deadline      TEXT,
            created_at    TEXT NOT NULL,
            status        TEXT DEFAULT 'active',
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT NOT NULL CHECK(type IN ('allowance','expense','savings')),
            amount      REAL NOT NULL CHECK(amount > 0),
            category    TEXT,
            date        TEXT NOT NULL,
            notes       TEXT,
            receipt     TEXT,
            recurring   TEXT DEFAULT 'none',
            goal_id     INTEGER,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (goal_id) REFERENCES savings_goals (id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT NOT NULL,
            attempt_time TEXT NOT NULL,
            success      INTEGER NOT NULL
        )
    """)

    # --- Safe migration: add receipt column to existing transactions tables ---
    existing_tx_cols = [r[1] for r in c.execute("PRAGMA table_info(transactions)").fetchall()]
    if "receipt" not in existing_tx_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN receipt TEXT")

    conn.commit()
    conn.close()

    # Take an automatic snapshot backup every time the schema is verified/updated
    # (i.e. on every startup). This never touches the live database file — it's a
    # read-only copy — so it can't ever cause data loss on its own.
    backup_db()


# =====================================================================================
# AUTOMATIC BACKUPS
# =====================================================================================
def backup_db():
    """Copy allowance.db into data/backups/ with a timestamped filename. Purely a
    read-only copy of the live database — never moves, deletes, or modifies the
    original. Keeps only the most recent MAX_DB_BACKUPS snapshots, pruning older
    ones. Returns the backup path, or None if there's no database yet to back up."""
    if not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUPS_DIR, f"allowance_{stamp}.db")
    try:
        shutil.copy2(DB_PATH, backup_path)
    except OSError:
        return None
    _prune_old_backups()
    return backup_path


def _prune_old_backups():
    """Keep only the MAX_DB_BACKUPS most recent snapshots in data/backups/."""
    try:
        backups = sorted(
            (f for f in os.listdir(BACKUPS_DIR) if f.startswith("allowance_") and f.endswith(".db")),
            reverse=True,
        )
        for old in backups[MAX_DB_BACKUPS:]:
            os.remove(os.path.join(BACKUPS_DIR, old))
    except FileNotFoundError:
        pass


# =====================================================================================
# ON-DISK FILE HELPERS (receipts / profile photos / exports)
# =====================================================================================
def save_file(directory, filename, raw_bytes):
    """Write raw_bytes to <directory>/<filename>, creating the directory if needed.
    Generic helper used for receipts, profile photos, and saved export copies.
    Returns the full path written."""
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, filename)
    with open(dest, "wb") as f:
        f.write(raw_bytes)
    return dest
