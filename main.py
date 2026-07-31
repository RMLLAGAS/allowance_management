#!/usr/bin/env python3
# =====================================================================================
#  ALLOWANCE MANAGEMENT  —  "Smart Personal Finance Manager"
#  Single-file Flask + SQLite application (Pydroid 3 compatible)
#
#  RUN:      python main.py
#  REQUIRES: pip install flask
#  NOTE:     Everything (backend, HTML, CSS, JS, DB layer) lives in this one file.
# =====================================================================================

# =====================================================================================
# SECTION 1 — IMPORTS
# =====================================================================================
import os
import re
import csv
import io
import json
import sqlite3
import secrets
import random
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    g, flash, jsonify, Response
)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

# Load variables from a local .env file (if present) into os.environ BEFORE
# anything below reads them with os.environ.get(). Without this, EMAIL_ADDRESS /
# EMAIL_APP_PASSWORD / SECRET_KEY / DB_PATH in your .env are silently ignored —
# this is the #1 cause of "verification email not sending" locally.
# On Render/Heroku this is a no-op (no .env file there; env vars are injected
# directly by the platform), so it's safe either way.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================================
# SECTION 2 — APP CONFIGURATION
# =====================================================================================
APP_NAME     = "Allowance Management"
APP_TAGLINE  = "Smart Personal Finance Manager"
APP_VERSION  = "1.5.1"
DEVELOPER    = "RM LLAGAS"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# On Render, set DB_PATH to a location on your mounted persistent disk (e.g. /var/data/allowance.db)
# so the database survives redeploys. Without it, the DB resets on every deploy.
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "allowance.db"))

app = Flask(__name__)
# On Render, set a SECRET_KEY environment variable so sessions survive restarts/redeploys.
# Falls back to a random key (fine for local/Pydroid use, but logs everyone out on each restart).
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    # 6MB is enough for the base64-encoded profile/cover photos and receipts,
    # which are the only file uploads this app handles.
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
)

CATEGORIES = ["Food", "School", "Transportation", "Bills", "Shopping", "Entertainment", "Others"]
SOURCES_HINT = ["Parents", "Salary", "Allowance", "Gift", "Freelance", "Other"]

# --- Email verification (Brevo HTTP API) ----------------------------------------------
# Render (and most free-tier hosts) block outbound SMTP ports (25/465/587), which is why
# smtplib connections fail with "[Errno 101] Network is unreachable" even with correct
# credentials. Brevo's API is called over plain HTTPS (port 443), which is never blocked.
# Unlike most providers, Brevo does NOT require owning/verifying a domain — you only verify
# one sender email address (click a link Brevo emails you), then you can send to ANY
# recipient. Free tier: 300 emails/day, no credit card, no expiration.
# 1. Sign up at https://brevo.com
# 2. Settings -> Senders, Domains & Dedicated IPs -> Senders -> Add a Sender (verify by
#    clicking the link Brevo sends to that email — e.g. use your own Gmail here)
# 3. Settings -> SMTP & API -> API Keys -> Generate a new API key
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
# Must be the exact email address you verified as a Sender in Brevo (step 2 above).
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
VERIFICATION_CODE_MINUTES = 10

# --- Login attempt limiter (in-memory, per-process) ---------------------------------
LOGIN_ATTEMPTS = {}     # { username_lower: [ (datetime, success_bool), ... ] }
MAX_ATTEMPTS   = 5
LOCKOUT_MINUTES = 15


# =====================================================================================
# SECTION 3 — DATABASE LAYER (SQLite)
# =====================================================================================
def get_db():
    """Return a request-scoped SQLite connection."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all required tables (idempotent) with proper foreign keys."""
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

    # --- Safe migration: add yearly_budget to existing installs that predate it ---
    # (Existing monthly_budget / weekly_budget / daily_budget columns and data are untouched.)
    existing_user_cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "yearly_budget" not in existing_user_cols:
        c.execute("ALTER TABLE users ADD COLUMN yearly_budget REAL DEFAULT 0")

    # --- Safe migration: add cover_photo + email verification columns for existing installs ---
    # cover_photo is stored the same way as profile_pic: a base64 data-URI string directly
    # in SQLite. No separate uploads folder, and the old background-video system (which used
    # to store bg_video_filename here) has been removed entirely.
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

    conn.commit()
    conn.close()


# =====================================================================================
# SECTION 4 — SECURITY HELPERS (CSRF / validation / rate limiting / auth)
# =====================================================================================
def csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("_csrf_token")
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not token or not sent or token != sent:
            flash("Security check failed. Please try again.", "danger")
            return redirect(request.referrer or url_for("login"))


@app.before_request
def session_activity_guard():
    """Auto-logout on inactivity, based on the user's configured session timeout."""
    if "user_id" in session:
        timeout_minutes = session.get("_timeout_minutes", 30)
        last_seen = session.get("_last_seen")
        now = datetime.now()
        if last_seen:
            elapsed = (now - datetime.fromisoformat(last_seen)).total_seconds() / 60
            if elapsed > timeout_minutes:
                session.clear()
                flash("You were logged out due to inactivity.", "warning")
                return redirect(url_for("login"))
        session["_last_seen"] = now.isoformat()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Guard against stale sessions pointing at a user_id that no longer
        # exists in the DB (e.g. DB got reset/redeployed while the browser
        # kept an old session cookie).
        db = get_db()
        user = db.execute("SELECT id FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user is None:
            session.clear()
            flash("Your session has expired. Please log in again.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def is_locked_out(username):
    key = username.lower().strip()
    attempts = LOGIN_ATTEMPTS.get(key, [])
    cutoff = datetime.now() - timedelta(minutes=LOCKOUT_MINUTES)
    recent_fails = [a for a in attempts if a[0] > cutoff and not a[1]]
    return len(recent_fails) >= MAX_ATTEMPTS


def record_attempt(username, success):
    key = username.lower().strip()
    LOGIN_ATTEMPTS.setdefault(key, []).append((datetime.now(), success))
    cutoff = datetime.now() - timedelta(minutes=LOCKOUT_MINUTES)
    LOGIN_ATTEMPTS[key] = [a for a in LOGIN_ATTEMPTS[key] if a[0] > cutoff]


def valid_username(u):
    return bool(re.fullmatch(r"[A-Za-z0-9_.]{3,20}", u or ""))


def password_strength(pw):
    """Return (score 0-4, list_of_missing_requirements)."""
    pw = pw or ""
    checks = {
        "At least 8 characters": len(pw) >= 8,
        "One uppercase letter": bool(re.search(r"[A-Z]", pw)),
        "One lowercase letter": bool(re.search(r"[a-z]", pw)),
        "One number": bool(re.search(r"\d", pw)),
        "One special character": bool(re.search(r"[^A-Za-z0-9]", pw)),
    }
    score = sum(checks.values())
    missing = [k for k, v in checks.items() if not v]
    return score, missing


def sanitize(text, max_len=500):
    if text is None:
        return ""
    return str(text).strip()[:max_len]


def parse_amount(raw):
    try:
        val = round(float(raw), 2)
        if val <= 0 or val > 100_000_000:
            return None
        return val
    except (TypeError, ValueError):
        return None


def parse_date(raw):
    try:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def encode_upload(file_storage, max_bytes=4 * 1024 * 1024, allowed=("png", "jpg", "jpeg", "gif", "webp")):
    """Validate + base64-encode an uploaded image so it can live inside SQLite (no static/ folder)."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        return None
    data = file_storage.read()
    if len(data) > max_bytes:
        return None
    import base64
    mime = "image/" + ("jpeg" if ext == "jpg" else ext)
    return f"data:{mime};base64,{base64.b64encode(data).decode('utf-8')}"


def valid_email(e):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e or ""))


def generate_verification_code():
    return f"{random.randint(0, 999999):06d}"


def send_verification_email(to_email, full_name, code):
    """Send the 6-digit verification code via the Brevo HTTP API (HTTPS, port 443).
    Returns True on success, False if sending failed (e.g. API key not configured)."""
    if not BREVO_API_KEY or not EMAIL_FROM:
        # Not configured — caller still lets the user reach the verify page
        # (useful for local/offline testing) but nothing gets sent.
        print("[email] BREVO_API_KEY / EMAIL_FROM not set — skipping send. "
              "Check your .env file (and that python-dotenv is installed).")
        return False
    try:
        subject = f"{APP_NAME} — Your verification code"
        text_body = (
            f"Hi {full_name},\n\n"
            f"Your {APP_NAME} verification code is: {code}\n\n"
            f"This code expires in {VERIFICATION_CODE_MINUTES} minutes.\n"
            f"If you didn't request this, you can ignore this email.\n"
        )
        payload = json.dumps({
            "sender": {"name": APP_NAME, "email": EMAIL_FROM},
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": text_body,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            method="POST",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Cloudflare/other WAFs in front of some APIs block the default
                # "Python-urllib/x.x" User-Agent. A normal-looking one avoids that.
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return True
            print(f"[email] Brevo API returned status {resp.status} for {to_email}")
            return False
    except urllib.error.HTTPError as exc:
        # Printed to the server console/log so the real API error (bad key,
        # unverified sender email, etc.) is visible instead of failing silently.
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"[email] Brevo API error sending to {to_email}: {exc.code} {detail}")
        return False
    except Exception as exc:
        print(f"[email] Failed to send verification email to {to_email}: {exc}")
        return False


def issue_verification_code(db, user_id, email, full_name):
    """Generate + store a fresh verification code/expiry for a user, email it, and
    return (code, sent_ok) — sent_ok is False if SMTP isn't configured or failed,
    so callers can flash a helpful warning instead of failing silently."""
    code = generate_verification_code()
    expiry = (datetime.now() + timedelta(minutes=VERIFICATION_CODE_MINUTES)).isoformat()
    db.execute(
        "UPDATE users SET verification_code=?, verification_expiry=? WHERE id=?",
        (code, expiry, user_id)
    )
    db.commit()
    sent_ok = send_verification_email(email, full_name, code)
    return code, sent_ok


# =====================================================================================
# SECTION 5 — DATA / QUERY HELPERS
# =====================================================================================
def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def get_stats(user_id):
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_start = datetime.now().strftime("%Y-%m-01")

    def total(ttype, since=None):
        q = "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type=?"
        params = [user_id, ttype]
        if since:
            q += " AND date >= ?"
            params.append(since)
        return db.execute(q, params).fetchone()["t"]

    total_allowance = total("allowance")
    total_expense = total("expense")
    total_savings = total("savings")
    today_expense = total("expense", today)
    week_expense = total("expense", week_start)
    month_expense = total("expense", month_start)
    month_allowance = total("allowance", month_start)

    balance = round(total_allowance - total_expense - total_savings, 2)
    savings_rate = round((total_savings / total_allowance * 100), 1) if total_allowance > 0 else 0.0

    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    monthly_budget = user["monthly_budget"] or 0

    if monthly_budget > 0:
        # Budget is configured: keep the normal Budget - Expenses calculation.
        budget_remaining = round(monthly_budget - month_expense, 2)
        budget_label = "Budget Remaining"
    else:
        # No budget configured yet: fall back to the current balance instead of
        # showing a negative "0 - expenses" value, which was confusing.
        budget_remaining = balance
        budget_label = "Remaining Balance"

    return {
        "balance": balance,
        "total_allowance": round(total_allowance, 2),
        "total_expense": round(total_expense, 2),
        "total_savings": round(total_savings, 2),
        "today_expense": round(today_expense, 2),
        "week_expense": round(week_expense, 2),
        "month_expense": round(month_expense, 2),
        "month_allowance": round(month_allowance, 2),
        "savings_rate": savings_rate,
        "monthly_budget": monthly_budget,
        "budget_remaining": budget_remaining,
        "budget_label": budget_label,
        "budget_pct": min(round((month_expense / monthly_budget * 100), 1), 999) if monthly_budget > 0 else 0,
    }


BUDGET_TYPES = ("yearly", "monthly", "weekly", "daily")


def budget_period_start(budget_type):
    """Return the ISO date string marking the start of the current period for a budget type."""
    now = datetime.now()
    if budget_type == "yearly":
        return now.strftime("%Y-01-01")
    if budget_type == "monthly":
        return now.strftime("%Y-%m-01")
    if budget_type == "weekly":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")  # daily


def get_budget_stats(user_id, budget_type):
    """Compute budget dashboard figures (spent/remaining/% used) for a single budget type.
    This is used only by the Budget page and does not affect get_stats() (dashboard/reports)."""
    if budget_type not in BUDGET_TYPES:
        budget_type = "monthly"

    db = get_db()
    period_start = budget_period_start(budget_type)

    period_expense = db.execute(
        "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='expense' AND date >= ?",
        (user_id, period_start)
    ).fetchone()["t"]

    period_count = db.execute(
        "SELECT COUNT(*) c FROM transactions WHERE user_id=? AND type='expense' AND date >= ?",
        (user_id, period_start)
    ).fetchone()["c"]

    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    column = f"{budget_type}_budget"
    budget_amount = user[column] or 0
    period_expense = round(period_expense, 2)
    budget_remaining = round(budget_amount - period_expense, 2)
    budget_pct = min(round((period_expense / budget_amount * 100), 1), 999) if budget_amount > 0 else 0

    return {
        "budget_type": budget_type,
        "period_expense": period_expense,
        "period_count": period_count,
        "budget_amount": round(budget_amount, 2),
        "budget_remaining": budget_remaining,
        "budget_pct": budget_pct,
    }


def chart_data(user_id):
    db = get_db()
    now = datetime.now()

    # --- Last 6 months: allowance vs expenses ---
    months, allowance_series, expense_series = [], [], []
    for i in range(5, -1, -1):
        m = (now.replace(day=1) - timedelta(days=1)) if i == 0 else now
        target = (now.replace(day=1) - timedelta(days=30 * i))
        label = target.strftime("%b %Y")
        ym = target.strftime("%Y-%m")
        months.append(label)
        allowance_series.append(db.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='allowance' AND date LIKE ?",
            (user_id, ym + "%")).fetchone()["t"])
        expense_series.append(db.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='expense' AND date LIKE ?",
            (user_id, ym + "%")).fetchone()["t"])

    # --- Category breakdown (expenses) ---
    cat_rows = db.execute(
        "SELECT category, COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='expense' GROUP BY category",
        (user_id,)).fetchall()
    cat_labels = [r["category"] or "Others" for r in cat_rows] or ["No data"]
    cat_values = [r["t"] for r in cat_rows] or [1]

    # --- Savings growth (cumulative, last 6 months) ---
    savings_growth = []
    running = 0
    for i in range(5, -1, -1):
        target = (now.replace(day=1) - timedelta(days=30 * i))
        ym = target.strftime("%Y-%m")
        month_amt = db.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='savings' AND date LIKE ?",
            (user_id, ym + "%")).fetchone()["t"]
        running += month_amt
        savings_growth.append(round(running, 2))

    # --- Weekly spending (last 7 days) ---
    week_labels, week_values = [], []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i))
        week_labels.append(d.strftime("%a"))
        week_values.append(db.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='expense' AND date=?",
            (user_id, d.strftime("%Y-%m-%d"))).fetchone()["t"])

    # --- Yearly summary (12 months, current year) ---
    year_labels, year_values = [], []
    for m in range(1, 13):
        ym = f"{now.year}-{m:02d}"
        year_labels.append(datetime(now.year, m, 1).strftime("%b"))
        year_values.append(db.execute(
            "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type='expense' AND date LIKE ?",
            (user_id, ym + "%")).fetchone()["t"])

    stats = get_stats(user_id)

    return {
        "months": months, "allowance_series": allowance_series, "expense_series": expense_series,
        "cat_labels": cat_labels, "cat_values": cat_values,
        "savings_growth": savings_growth,
        "week_labels": week_labels, "week_values": week_values,
        "year_labels": year_labels, "year_values": year_values,
        "budget_used": stats["budget_pct"], "budget_free": max(100 - stats["budget_pct"], 0),
    }


# =====================================================================================
# SECTION 6 — HTML TEMPLATES  (inline, via Jinja2 DictLoader — no templates/ folder)
# =====================================================================================

BASE_HTML = """
<!DOCTYPE html>
<html lang="en" data-theme="{{ (user.theme if user else 'blue') }}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>{% block title %}{{ app_name }}{% endblock %} · {{ app_name }}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💳</text></svg>">
<link rel="manifest" href="{{ url_for('manifest') }}">
<link rel="apple-touch-icon" href="{{ url_for('apple_touch_icon') }}">
<meta name="theme-color" content="#0b1d3a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="{{ app_name }}">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg-deep:#060f24; --bg-mid:#0b1d3a; --bg-grad-end:#123a75;
  --accent-1:#3b82f6; --accent-2:#22d3ee; --accent-3:#6366f1;
  --glass:rgba(255,255,255,.07); --glass-brd:rgba(255,255,255,.14);
  --text-hi:#f4f8ff; --text-lo:#9fb3d1;
  --success:#22c55e; --danger:#f43f5e; --warning:#f59e0b;
  --radius:20px; --radius-sm:14px;
}
html[data-theme="light"]{
  --bg-deep:#eef3fb; --bg-mid:#dfe9fb; --bg-grad-end:#cfe0fb;
  --glass:rgba(255,255,255,.65); --glass-brd:rgba(20,40,80,.10);
  --text-hi:#0b1d3a; --text-lo:#4a5d80;
}
*{box-sizing:border-box}
body{
  margin:0; min-height:100vh; font-family:'Inter',sans-serif; color:var(--text-hi);
  background:
    radial-gradient(circle at 15% 0%, rgba(59,130,246,.35), transparent 45%),
    radial-gradient(circle at 90% 10%, rgba(34,211,238,.22), transparent 40%),
    linear-gradient(160deg, var(--bg-deep), var(--bg-mid) 55%, var(--bg-grad-end));
  background-attachment:fixed;
  padding-bottom:92px;
  -webkit-font-smoothing:antialiased;
}
h1,h2,h3,h4,h5,.brand,.display-font{font-family:'Plus Jakarta Sans',sans-serif;}
a{text-decoration:none}
.container-app{max-width:480px; margin:0 auto; padding:18px 16px 8px;}
.glass{
  background:var(--glass); border:1px solid var(--glass-brd);
  backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px);
  border-radius:var(--radius); box-shadow:0 8px 32px rgba(0,0,0,.25);
}
.card-app{ padding:18px; margin-bottom:14px; animation:fadeUp .45s ease both; }
.grad-btn{
  background:linear-gradient(135deg, var(--accent-1), var(--accent-2));
  border:none; color:#04121f; font-weight:700; border-radius:16px;
  padding:13px 18px; box-shadow:0 10px 24px rgba(59,130,246,.35);
  transition:transform .15s ease, box-shadow .15s ease;
}
.grad-btn:hover{ transform:translateY(-2px); box-shadow:0 14px 30px rgba(59,130,246,.45); color:#04121f;}
.btn-ghost{
  background:var(--glass); border:1px solid var(--glass-brd); color:var(--text-hi);
  border-radius:16px; font-weight:600;
}
.form-control, .form-select{
  background:rgba(255,255,255,.06); border:1px solid var(--glass-brd); color:var(--text-hi);
  border-radius:14px; padding:11px 14px;
}
.form-control:focus, .form-select:focus{
  background:rgba(255,255,255,.09); color:var(--text-hi); border-color:var(--accent-2);
  box-shadow:0 0 0 .18rem rgba(34,211,238,.20);
}
.form-control::placeholder{color:var(--text-lo)}
label.form-label{color:var(--text-lo); font-size:.83rem; font-weight:600; letter-spacing:.02em;}
.text-lo{color:var(--text-lo)}
.brand-badge{
  width:46px;height:46px;border-radius:14px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,var(--accent-1),var(--accent-3)); font-size:22px; box-shadow:0 8px 20px rgba(59,130,246,.4);
}
.stat-card{ position:relative; overflow:hidden; }
.stat-card .icon-chip{
  width:38px;height:38px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  background:rgba(255,255,255,.10); font-size:17px;
}
.stat-value{font-family:'Plus Jakarta Sans',sans-serif; font-weight:800; font-size:1.5rem;}
.pill{ border-radius:999px; padding:4px 12px; font-size:.72rem; font-weight:700; letter-spacing:.02em;}
.pill-success{background:rgba(34,197,94,.15); color:var(--success);}
.pill-danger{background:rgba(244,63,94,.15); color:var(--danger);}
.pill-warning{background:rgba(245,158,11,.15); color:var(--warning);}
.pill-info{background:rgba(59,130,246,.15); color:var(--accent-1);}
.avatar{
  width:48px;height:48px;border-radius:50%; object-fit:cover; border:2px solid var(--glass-brd);
  background:linear-gradient(135deg,var(--accent-1),var(--accent-3));
  display:flex;align-items:center;justify-content:center; font-weight:700; color:#fff;
}
.progress{ background:rgba(255,255,255,.10); border-radius:999px; height:10px; overflow:hidden;}
.progress-bar{ background:linear-gradient(90deg,var(--accent-1),var(--accent-2)); }
.bottom-nav{
  position:fixed; bottom:0; left:0; right:0; z-index:1030;
  background:rgba(8,17,38,.85); backdrop-filter:blur(20px); border-top:1px solid var(--glass-brd);
  padding:8px 6px calc(8px + env(safe-area-inset-bottom));
}
html[data-theme="light"] .bottom-nav{background:rgba(255,255,255,.85);}
.bottom-nav .nav-item-app{
  flex:1; text-align:center; color:var(--text-lo); font-size:.68rem; font-weight:600; padding:6px 2px; border-radius:14px;
}
.bottom-nav .nav-item-app i{display:block; font-size:1.28rem; margin-bottom:2px;}
.bottom-nav .nav-item-app.active{ color:var(--accent-2); background:rgba(34,211,238,.10);}
.top-bar{ display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; }
.icon-btn{
  width:40px;height:40px;border-radius:12px; display:flex;align-items:center;justify-content:center;
  background:var(--glass); border:1px solid var(--glass-brd); color:var(--text-hi); position:relative;
}
.dot{position:absolute; top:6px; right:7px; width:8px; height:8px; border-radius:50%; background:var(--danger); border:1.5px solid var(--bg-deep);}
.skeleton{ background:linear-gradient(90deg, rgba(255,255,255,.06) 25%, rgba(255,255,255,.14) 37%, rgba(255,255,255,.06) 63%);
  background-size:400% 100%; animation:shimmer 1.4s ease infinite; border-radius:12px;}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
@keyframes fadeUp{from{opacity:0; transform:translateY(14px)} to{opacity:1; transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0} to{opacity:1}}
.fade-in{animation:fadeIn .4s ease both;}
.toast-stack{position:fixed; top:14px; left:0; right:0; z-index:2000; display:flex; flex-direction:column; align-items:center; gap:8px; pointer-events:none;}
.toast-app{
  pointer-events:auto; min-width:250px; max-width:92vw; padding:12px 16px; border-radius:14px; color:#fff; font-weight:600; font-size:.88rem;
  box-shadow:0 10px 26px rgba(0,0,0,.35); animation:toastIn .35s ease both; display:flex; align-items:center; gap:10px;
}
@keyframes toastIn{from{opacity:0; transform:translateY(-16px)} to{opacity:1; transform:translateY(0)}}
.toast-success{background:linear-gradient(135deg,#16a34a,#22c55e)}
.toast-danger{background:linear-gradient(135deg,#e11d48,#f43f5e)}
.toast-warning{background:linear-gradient(135deg,#d97706,#f59e0b)}
.toast-info{background:linear-gradient(135deg,#2563eb,#3b82f6)}
.progress-ring circle{transition:stroke-dashoffset .6s ease}
.section-title{font-family:'Plus Jakarta Sans',sans-serif; font-weight:800; font-size:1.05rem; margin-bottom:12px;}
.quick-action{ text-align:center; padding:14px 6px; }
.quick-action .icon-chip{ width:48px;height:48px; margin:0 auto 8px; font-size:20px;}
.quick-action span{font-size:.74rem; font-weight:700; display:block;}
.list-tx{display:flex; align-items:center; gap:12px; padding:11px 0; border-bottom:1px solid var(--glass-brd);}
.list-tx:last-child{border-bottom:none}
.tx-icon{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;justify-content:center; font-size:16px; flex-shrink:0;}
.page-transition{animation:fadeUp .35s ease both;}
.spinner-btn{width:16px;height:16px;border:2px solid rgba(255,255,255,.5); border-top-color:#04121f; border-radius:50%; animation:spin .6s linear infinite; display:inline-block;}
@keyframes spin{to{transform:rotate(360deg)}}
::selection{background:var(--accent-2); color:#04121f;}
.link-accent{color:var(--accent-2); font-weight:600;}
.strength-bar{height:6px; border-radius:99px; background:rgba(255,255,255,.1); overflow:hidden; flex:1;}
.strength-fill{height:100%; width:0%; transition:width .25s ease, background .25s ease;}
/* --- Welcome Profile Card cover photo background --- */
.welcome-card{position:relative; overflow:hidden;}
.welcome-bg-photo{
  position:absolute; top:0; left:0; width:100%; height:100%; object-fit:cover;
  z-index:0; border-radius:var(--radius); opacity:0; animation:coverFadeIn .5s ease forwards;
}
@keyframes coverFadeIn{from{opacity:0} to{opacity:1}}
.welcome-bg-overlay{position:absolute; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,.5); z-index:1; pointer-events:none;}
.welcome-card-content{position:relative; z-index:2; width:100%;}
.cover-photo-preview{
  width:100%; height:120px; border-radius:16px; object-fit:cover; border:1px solid var(--glass-brd);
  background:rgba(255,255,255,.06);
}
.upload-spinner-overlay{
  position:absolute; inset:0; z-index:5; display:flex; align-items:center; justify-content:center;
  background:rgba(0,0,0,.45); border-radius:16px;
}
.upload-spinner{width:26px;height:26px;border:3px solid rgba(255,255,255,.4); border-top-color:#fff; border-radius:50%; animation:spin .7s linear infinite;}
</style>
{% block extra_head %}{% endblock %}
</head>
<body>

<div class="toast-stack" id="toastStack"></div>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <script>
      window.__flash = {{ messages|tojson }};
    </script>
  {% endif %}
{% endwith %}

{% block body %}{% endblock %}

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ---------- Toast engine ----------
function showToast(message, type){
  type = type || 'info';
  const icons = {success:'bi-check-circle-fill', danger:'bi-x-circle-fill', warning:'bi-exclamation-triangle-fill', info:'bi-info-circle-fill'};
  const el = document.createElement('div');
  el.className = 'toast-app toast-' + type;
  el.innerHTML = '<i class="bi ' + (icons[type]||icons.info) + '"></i><span>' + message + '</span>';
  document.getElementById('toastStack').appendChild(el);
  setTimeout(()=>{ el.style.transition='opacity .3s ease, transform .3s ease'; el.style.opacity='0'; el.style.transform='translateY(-10px)'; setTimeout(()=>el.remove(),300); }, 3200);
}
document.addEventListener('DOMContentLoaded', function(){
  if(window.__flash){
    window.__flash.forEach(function(pair){
      let cat = pair[0], msg = pair[1];
      if(cat === 'message') cat = 'info';
      showToast(msg, cat);
    });
  }
});
// ---------- Loading-button helper ----------
function setBtnLoading(btn, loadingText){
  btn.dataset.original = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-btn"></span> ' + (loadingText || 'Please wait...');
  btn.disabled = true;
}
document.addEventListener('submit', function(e){
  const form = e.target;
  const btn = form.querySelector('button[type="submit"]');
  if(btn && !btn.dataset.noSpin) setBtnLoading(btn, btn.dataset.loading || 'Processing...');
});
// ---------- PWA service worker registration (enables "Add to Home Screen" / APK packaging) ----------
if('serviceWorker' in navigator){
  window.addEventListener('load', function(){
    navigator.serviceWorker.register('{{ url_for("service_worker") }}').catch(function(){});
  });
}
</script>
{% block extra_scripts %}{% endblock %}
</body>
</html>
"""

# --- Shared "app shell" (header + bottom nav) used by every logged-in page ----------
APP_SHELL_HEAD = """
{% block body %}
<div class="container-app page-transition">
  <div class="top-bar">
    <a href="{{ url_for('dashboard') }}" class="d-flex align-items-center text-decoration-none gap-2">
      <span class="brand-badge">💳</span>
      <div>
        <div class="brand fw-bold" style="font-size:1.02rem; line-height:1;">{{ app_name }}</div>
        <div class="text-lo" style="font-size:.68rem;">{{ app_tagline }}</div>
      </div>
    </a>
    <div class="d-flex gap-2">
      <a href="{{ url_for('reports') }}" class="icon-btn" title="Notifications">
        <i class="bi bi-bell"></i>
        {% if notif_count and notif_count > 0 %}<span class="dot"></span>{% endif %}
      </a>
      <a href="{{ url_for('settings') }}" class="icon-btn" title="Settings">
        <i class="bi bi-three-dots-vertical"></i>
      </a>
    </div>
  </div>
"""

APP_SHELL_TAIL = """
</div>
<nav class="bottom-nav d-flex">
  <a href="{{ url_for('dashboard') }}" class="nav-item-app {{ 'active' if active=='home' }}"><i class="bi bi-house-door-fill"></i>Home</a>
  <a href="{{ url_for('transactions') }}" class="nav-item-app {{ 'active' if active=='transactions' }}"><i class="bi bi-arrow-left-right"></i>Activity</a>
  <a href="{{ url_for('budget') }}" class="nav-item-app {{ 'active' if active=='budget' }}"><i class="bi bi-pie-chart-fill"></i>Budget</a>
  <a href="{{ url_for('savings') }}" class="nav-item-app {{ 'active' if active=='savings' }}"><i class="bi bi-piggy-bank-fill"></i>Savings</a>
  <a href="{{ url_for('reports') }}" class="nav-item-app {{ 'active' if active=='reports' }}"><i class="bi bi-bar-chart-line-fill"></i>Reports</a>
</nav>
{% endblock %}
"""

# --- LOGIN ----------------------------------------------------------------------------
LOGIN_HTML = """
{% extends "base.html" %}
{% block title %}Login{% endblock %}
{% block body %}
<div class="container-app d-flex flex-column justify-content-center fade-in" style="min-height:100vh; padding-bottom:40px;">
  <div class="text-center mb-4">
    <div class="brand-badge mx-auto mb-3" style="width:64px;height:64px;font-size:30px;border-radius:20px;">💳</div>
    <h2 class="fw-bold mb-0">{{ app_name }}</h2>
    <p class="text-lo mb-0">{{ app_tagline }}</p>
  </div>

  <div class="glass card-app">
    <h5 class="fw-bold mb-3">Welcome back</h5>
    <form method="POST" action="{{ url_for('login') }}" id="loginForm" novalidate>
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">USERNAME</label>
        <input type="text" name="username" class="form-control" placeholder="Enter your username" required autocomplete="username" value="{{ request.form.get('username','') }}">
      </div>
      <div class="mb-2">
        <label class="form-label">PASSWORD</label>
        <div class="position-relative">
          <input type="password" name="password" id="loginPw" class="form-control" placeholder="Enter your password" required autocomplete="current-password">
          <button type="button" class="btn btn-sm position-absolute top-50 end-0 translate-middle-y me-1 text-lo" onclick="togglePw('loginPw', this)" data-no-spin="1"><i class="bi bi-eye"></i></button>
        </div>
      </div>
      <div class="d-flex justify-content-between align-items-center mb-3">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" name="remember" id="remember">
          <label class="form-check-label text-lo" for="remember" style="font-size:.85rem;">Remember me</label>
        </div>
        <a href="{{ url_for('forgot_password') }}" class="link-accent" style="font-size:.85rem;">Forgot password?</a>
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Signing in...">Sign In</button>
    </form>
  </div>

  <p class="text-center text-lo mt-4 mb-0">Don't have an account?
    <a href="{{ url_for('register') }}" class="link-accent">Create one</a>
  </p>
</div>
<script>
function togglePw(id, btn){
  const inp = document.getElementById(id);
  const icon = btn.querySelector('i');
  if(inp.type === 'password'){ inp.type='text'; icon.className='bi bi-eye-slash'; }
  else { inp.type='password'; icon.className='bi bi-eye'; }
}
</script>
{% endblock %}
"""

# --- REGISTER ---------------------------------------------------------------------------
REGISTER_HTML = """
{% extends "base.html" %}
{% block title %}Create Account{% endblock %}
{% block body %}
<div class="container-app fade-in" style="padding-top:32px; padding-bottom:40px;">
  <div class="text-center mb-4">
    <div class="brand-badge mx-auto mb-3" style="width:64px;height:64px;font-size:30px;border-radius:20px;">💳</div>
    <h2 class="fw-bold mb-0">Create Account</h2>
    <p class="text-lo mb-0">Start managing your money smarter</p>
  </div>

  <div class="glass card-app">
    <form method="POST" action="{{ url_for('register') }}" id="regForm" novalidate>
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

      <div class="mb-3">
        <label class="form-label">FULL NAME</label>
        <input type="text" name="full_name" class="form-control" placeholder="Juan Dela Cruz" required maxlength="80" value="{{ request.form.get('full_name','') }}">
      </div>

      <div class="mb-3">
        <label class="form-label">USERNAME</label>
        <input type="text" name="username" id="regUsername" class="form-control" placeholder="e.g. juan_dc" required maxlength="20" value="{{ request.form.get('username','') }}">
        <div id="usernameStatus" class="form-text" style="min-height:18px;"></div>
      </div>

      <div class="mb-3">
        <label class="form-label">EMAIL ADDRESS</label>
        <input type="email" name="email" class="form-control" placeholder="juan@example.com" required maxlength="120" value="{{ request.form.get('email','') }}">
      </div>

      <div class="mb-2">
        <label class="form-label">PASSWORD</label>
        <div class="position-relative">
          <input type="password" name="password" id="regPw" class="form-control" placeholder="Create a strong password" required autocomplete="new-password">
          <button type="button" class="btn btn-sm position-absolute top-50 end-0 translate-middle-y me-1 text-lo" onclick="togglePw('regPw', this)" data-no-spin="1"><i class="bi bi-eye"></i></button>
        </div>
        <div class="d-flex align-items-center gap-2 mt-2">
          <div class="strength-bar"><div class="strength-fill" id="strengthFill"></div></div>
          <span class="text-lo" id="strengthLabel" style="font-size:.72rem; width:56px;">Weak</span>
        </div>
        <ul class="text-lo mt-2 mb-0" id="pwRules" style="font-size:.74rem; padding-left:18px;">
          <li id="r-len">At least 8 characters</li>
          <li id="r-up">One uppercase letter</li>
          <li id="r-low">One lowercase letter</li>
          <li id="r-num">One number</li>
          <li id="r-sp">One special character</li>
        </ul>
      </div>

      <div class="mb-3">
        <label class="form-label">CONFIRM PASSWORD</label>
        <input type="password" name="confirm_password" id="regPw2" class="form-control" placeholder="Re-enter password" required autocomplete="new-password">
        <div id="matchStatus" class="form-text" style="min-height:18px;"></div>
      </div>

      <button type="submit" class="grad-btn w-100" data-loading="Creating account...">Create Account</button>
    </form>
  </div>

  <p class="text-center text-lo mt-4 mb-0">Already have an account?
    <a href="{{ url_for('login') }}" class="link-accent">Sign in</a>
  </p>
</div>
<script>
function togglePw(id, btn){
  const inp = document.getElementById(id);
  const icon = btn.querySelector('i');
  if(inp.type === 'password'){ inp.type='text'; icon.className='bi bi-eye-slash'; }
  else { inp.type='password'; icon.className='bi bi-eye'; }
}
const pw = document.getElementById('regPw');
pw.addEventListener('input', function(){
  const v = pw.value;
  const rules = {
    'r-len': v.length >= 8, 'r-up': /[A-Z]/.test(v), 'r-low': /[a-z]/.test(v),
    'r-num': /\\d/.test(v), 'r-sp': /[^A-Za-z0-9]/.test(v)
  };
  let score = 0;
  Object.keys(rules).forEach(id => {
    const el = document.getElementById(id);
    if(rules[id]){ el.style.color = '#22c55e'; el.style.textDecoration='line-through'; score++; }
    else { el.style.color=''; el.style.textDecoration='none'; }
  });
  const pct = (score/5)*100;
  const fill = document.getElementById('strengthFill');
  fill.style.width = pct + '%';
  const label = document.getElementById('strengthLabel');
  const colors = ['#f43f5e','#f43f5e','#f59e0b','#f59e0b','#22c55e'];
  fill.style.background = colors[Math.max(score-1,0)];
  label.textContent = ['Very weak','Weak','Fair','Good','Strong'][Math.max(score-1,0)] || 'Very weak';
});
document.getElementById('regPw2').addEventListener('input', function(){
  const status = document.getElementById('matchStatus');
  if(this.value.length === 0){ status.textContent=''; return; }
  if(this.value === pw.value){ status.textContent='Passwords match'; status.style.color='#22c55e'; }
  else { status.textContent='Passwords do not match'; status.style.color='#f43f5e'; }
});
let userTimer;
document.getElementById('regUsername').addEventListener('input', function(){
  const val = this.value; const status = document.getElementById('usernameStatus');
  clearTimeout(userTimer);
  if(val.length < 3){ status.textContent=''; return; }
  status.textContent='Checking availability...'; status.style.color='#9fb3d1';
  userTimer = setTimeout(()=>{
    fetch("{{ url_for('api_check_username') }}?username=" + encodeURIComponent(val))
      .then(r=>r.json()).then(data=>{
        if(!data.valid){ status.textContent='3-20 chars: letters, numbers, _ or . only'; status.style.color='#f43f5e'; }
        else if(data.available){ status.textContent='Username is available'; status.style.color='#22c55e'; }
        else { status.textContent='Username already taken'; status.style.color='#f43f5e'; }
      });
  }, 350);
});
</script>
{% endblock %}
"""

# --- VERIFY EMAIL ----------------------------------------------------------------------
VERIFY_HTML = """
{% extends "base.html" %}
{% block title %}Verify Email{% endblock %}
{% block body %}
<div class="container-app d-flex flex-column justify-content-center fade-in" style="min-height:100vh; padding-bottom:40px;">
  <div class="text-center mb-4">
    <div class="brand-badge mx-auto mb-3" style="width:64px;height:64px;font-size:30px;border-radius:20px;"><i class="bi bi-envelope-check-fill"></i></div>
    <h2 class="fw-bold mb-0">Verify your email</h2>
    <p class="text-lo mb-0">We sent a 6-digit code to {{ pending_email }}</p>
  </div>

  <div class="glass card-app">
    <form method="POST" action="{{ url_for('verify_email') }}" novalidate>
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">VERIFICATION CODE</label>
        <input type="text" name="code" class="form-control text-center" style="letter-spacing:.4em; font-size:1.3rem;" placeholder="000000" required maxlength="6" inputmode="numeric" pattern="[0-9]{6}" autofocus>
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Verifying...">Verify Email</button>
    </form>
    <form method="POST" action="{{ url_for('resend_code') }}" class="mt-2">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button type="submit" class="btn-ghost w-100 py-2" data-loading="Sending...">Resend Code</button>
    </form>
  </div>

  <p class="text-center text-lo mt-4 mb-0"><a href="{{ url_for('login') }}" class="link-accent"><i class="bi bi-arrow-left"></i> Back to login</a></p>
</div>
{% endblock %}
"""

# --- FORGOT PASSWORD ------------------------------------------------------------------
FORGOT_HTML = """
{% extends "base.html" %}
{% block title %}Forgot Password{% endblock %}
{% block body %}
<div class="container-app fade-in" style="padding-top:60px;">
  <div class="text-center mb-4">
    <div class="brand-badge mx-auto mb-3" style="width:64px;height:64px;font-size:28px;border-radius:20px;"><i class="bi bi-key-fill"></i></div>
    <h2 class="fw-bold mb-0">Reset Password</h2>
    <p class="text-lo mb-0">We'll help you get back in</p>
  </div>
  <div class="glass card-app">
    <form method="POST" action="{{ url_for('forgot_password') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">USERNAME</label>
        <input type="text" name="username" class="form-control" placeholder="Enter your username" required>
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Sending...">Send Reset Instructions</button>
    </form>
  </div>
  <p class="text-center mt-4"><a href="{{ url_for('login') }}" class="link-accent"><i class="bi bi-arrow-left"></i> Back to login</a></p>
</div>
{% endblock %}
"""

# --- DASHBOARD / HOME -------------------------------------------------------------------
DASHBOARD_HTML = """
{% extends "base.html" %}
{% block title %}Dashboard{% endblock %}
""" + APP_SHELL_HEAD + """

  <div class="glass card-app welcome-card">
    {% if user.cover_photo %}
      <img class="welcome-bg-photo" src="{{ user.cover_photo }}" alt="">
      <div class="welcome-bg-overlay"></div>
    {% endif %}
    <div class="d-flex align-items-center gap-3 welcome-card-content">
      {% if user.profile_pic %}
        <img src="{{ user.profile_pic }}" class="avatar">
      {% else %}
        <div class="avatar">{{ user.full_name[0]|upper }}</div>
      {% endif %}
      <div class="flex-grow-1">
        <div class="text-lo" style="font-size:.78rem;">Welcome back,</div>
        <div class="fw-bold" style="font-size:1.05rem;">{{ user.full_name }}</div>
        <div class="text-lo" style="font-size:.72rem;">@{{ user.username }}</div>
      </div>
      <div class="text-end">
        <div class="fw-semibold" id="clockTime" style="font-size:.95rem;"></div>
        <div class="text-lo" id="clockDate" style="font-size:.68rem;"></div>
      </div>
    </div>
  </div>

  <div class="glass card-app" style="background:linear-gradient(135deg, rgba(59,130,246,.28), rgba(34,211,238,.14));">
    <div class="text-lo mb-1" style="font-size:.78rem;">CURRENT BALANCE</div>
    <div class="stat-value" style="font-size:2.1rem;">₱{{ '%.2f'|format(stats.balance) }}</div>
    <div class="d-flex gap-2 mt-2">
      <span class="pill pill-success"><i class="bi bi-arrow-down-left"></i> ₱{{ '%.2f'|format(stats.total_allowance) }} in</span>
      <span class="pill pill-danger"><i class="bi bi-arrow-up-right"></i> ₱{{ '%.2f'|format(stats.total_expense) }} out</span>
    </div>
  </div>

  <div class="row g-2 mb-2">
    <div class="col-6">
      <div class="glass card-app stat-card mb-0">
        <div class="icon-chip mb-2"><i class="bi bi-calendar-day" style="color:var(--accent-2)"></i></div>
        <div class="text-lo" style="font-size:.72rem;">Today's Expenses</div>
        <div class="stat-value" style="font-size:1.15rem;">₱{{ '%.2f'|format(stats.today_expense) }}</div>
      </div>
    </div>
    <div class="col-6">
      <div class="glass card-app stat-card mb-0">
        <div class="icon-chip mb-2"><i class="bi bi-piggy-bank" style="color:var(--success)"></i></div>
        <div class="text-lo" style="font-size:.72rem;">Savings</div>
        <div class="stat-value" style="font-size:1.15rem;">₱{{ '%.2f'|format(stats.total_savings) }}</div>
      </div>
    </div>
    <div class="col-6">
      <div class="glass card-app stat-card mb-0">
        <div class="icon-chip mb-2"><i class="bi bi-wallet2" style="color:var(--warning)"></i></div>
        <div class="text-lo" style="font-size:.72rem;">{{ stats.budget_label }}</div>
        <div class="stat-value" style="font-size:1.15rem;">₱{{ '%.2f'|format(stats.budget_remaining) }}</div>
      </div>
    </div>
    <div class="col-6">
      <div class="glass card-app stat-card mb-0">
        <div class="icon-chip mb-2"><i class="bi bi-graph-up-arrow" style="color:var(--accent-1)"></i></div>
        <div class="text-lo" style="font-size:.72rem;">Savings Rate</div>
        <div class="stat-value" style="font-size:1.15rem;">{{ stats.savings_rate }}%</div>
      </div>
    </div>
  </div>

  <div class="glass card-app">
    <div class="d-flex justify-content-between mb-2">
      <span class="text-lo" style="font-size:.78rem;">Weekly Spending</span>
      <span class="fw-semibold" style="font-size:.85rem;">₱{{ '%.2f'|format(stats.week_expense) }}</span>
    </div>
    <div class="d-flex justify-content-between mb-1">
      <span class="text-lo" style="font-size:.78rem;">Monthly Spending</span>
      <span class="fw-semibold" style="font-size:.85rem;">₱{{ '%.2f'|format(stats.month_expense) }}</span>
    </div>
  </div>

  <div class="section-title">Quick Actions</div>
  <div class="row g-2 mb-3">
    <div class="col-3"><a href="{{ url_for('add_allowance') }}" class="glass quick-action d-block text-decoration-none">
      <div class="icon-chip" style="background:rgba(34,197,94,.15)"><i class="bi bi-plus-circle-fill" style="color:var(--success)"></i></div>
      <span class="text-hi">Allowance</span></a></div>
    <div class="col-3"><a href="{{ url_for('add_expense') }}" class="glass quick-action d-block text-decoration-none">
      <div class="icon-chip" style="background:rgba(244,63,94,.15)"><i class="bi bi-dash-circle-fill" style="color:var(--danger)"></i></div>
      <span class="text-hi">Expense</span></a></div>
    <div class="col-3"><a href="{{ url_for('savings') }}" class="glass quick-action d-block text-decoration-none">
      <div class="icon-chip" style="background:rgba(59,130,246,.15)"><i class="bi bi-piggy-bank-fill" style="color:var(--accent-1)"></i></div>
      <span class="text-hi">Transfer</span></a></div>
    <div class="col-3"><a href="{{ url_for('budget') }}" class="glass quick-action d-block text-decoration-none">
      <div class="icon-chip" style="background:rgba(245,158,11,.15)"><i class="bi bi-sliders" style="color:var(--warning)"></i></div>
      <span class="text-hi">Budget</span></a></div>
  </div>

  <div class="glass card-app">
    <div class="d-flex justify-content-between align-items-center mb-1">
      <span class="section-title mb-0">Recent Transactions</span>
      <a href="{{ url_for('transactions') }}" class="link-accent" style="font-size:.8rem;">See all</a>
    </div>
    {% if recent %}
      {% for t in recent %}
      <div class="list-tx">
        <div class="tx-icon" style="background:{{ 'rgba(34,197,94,.15)' if t.type=='allowance' else ('rgba(244,63,94,.15)' if t.type=='expense' else 'rgba(59,130,246,.15)') }};">
          <i class="bi {{ 'bi-arrow-down-left' if t.type=='allowance' else ('bi-cart3' if t.type=='expense' else 'bi-piggy-bank') }}"
             style="color:{{ 'var(--success)' if t.type=='allowance' else ('var(--danger)' if t.type=='expense' else 'var(--accent-1)') }}"></i>
        </div>
        <div class="flex-grow-1">
          <div class="fw-semibold" style="font-size:.88rem;">{{ t.category or t.type|capitalize }}</div>
          <div class="text-lo" style="font-size:.72rem;">{{ t.date }}</div>
        </div>
        <div class="fw-bold" style="font-size:.9rem; color:{{ 'var(--success)' if t.type=='allowance' else 'var(--danger)' }}">
          {{ '+' if t.type=='allowance' else '-' }}₱{{ '%.2f'|format(t.amount) }}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="text-center text-lo py-4">
        <i class="bi bi-inboxes" style="font-size:2rem;"></i>
        <p class="mb-0 mt-2">No transactions yet. Add your first one!</p>
      </div>
    {% endif %}
  </div>
""" + APP_SHELL_TAIL + """
{% block extra_scripts %}
<script>
function updateClock(){
  const now = new Date();
  document.getElementById('clockTime').textContent = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  document.getElementById('clockDate').textContent = now.toLocaleDateString([], {month:'short', day:'numeric', year:'numeric'});
}
updateClock(); setInterval(updateClock, 1000*30);
</script>
{% endblock %}
"""

# --- ADD ALLOWANCE ----------------------------------------------------------------------
ADD_ALLOWANCE_HTML = """
{% extends "base.html" %}
{% block title %}Add Allowance{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <h5 class="fw-bold mb-3"><i class="bi bi-plus-circle-fill text-success"></i> Add Allowance</h5>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">AMOUNT (₱)</label>
        <input type="number" step="0.01" min="0.01" name="amount" class="form-control" placeholder="0.00" required>
      </div>
      <div class="mb-3">
        <label class="form-label">SOURCE</label>
        <input list="sourceList" name="source" class="form-control" placeholder="e.g. Parents, Salary" required>
        <datalist id="sourceList">{% for s in sources %}<option value="{{ s }}">{% endfor %}</datalist>
      </div>
      <div class="mb-3">
        <label class="form-label">DATE</label>
        <input type="date" name="date" class="form-control" value="{{ today }}" required>
      </div>
      <div class="mb-3">
        <label class="form-label">NOTES</label>
        <textarea name="notes" class="form-control" rows="2" placeholder="Optional note" maxlength="300"></textarea>
      </div>
      <div class="mb-3">
        <label class="form-label">RECURRING</label>
        <select name="recurring" class="form-select">
          <option value="none">One-time</option>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
        </select>
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Saving...">Save Allowance</button>
    </form>
  </div>
""" + APP_SHELL_TAIL

# --- ADD EXPENSE -------------------------------------------------------------------------
ADD_EXPENSE_HTML = """
{% extends "base.html" %}
{% block title %}Add Expense{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <h5 class="fw-bold mb-3"><i class="bi bi-dash-circle-fill text-danger"></i> Add Expense</h5>
    <form method="POST" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">AMOUNT (₱)</label>
        <input type="number" step="0.01" min="0.01" name="amount" class="form-control" placeholder="0.00" required>
      </div>
      <div class="mb-3">
        <label class="form-label">CATEGORY</label>
        <select name="category" class="form-select" required>
          {% for c in categories %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
        </select>
      </div>
      <div class="mb-3">
        <label class="form-label">DATE</label>
        <input type="date" name="date" class="form-control" value="{{ today }}" required>
      </div>
      <div class="mb-3">
        <label class="form-label">NOTES</label>
        <textarea name="notes" class="form-control" rows="2" placeholder="Optional note" maxlength="300"></textarea>
      </div>
      <div class="mb-3">
        <label class="form-label">RECEIPT (optional)</label>
        <input type="file" name="receipt" class="form-control" accept="image/*">
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Saving...">Save Expense</button>
    </form>
  </div>
""" + APP_SHELL_TAIL

# --- TRANSACTIONS -------------------------------------------------------------------------
TRANSACTIONS_HTML = """
{% extends "base.html" %}
{% block title %}Transactions{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <h5 class="fw-bold mb-3">Transaction History</h5>
    <form method="GET" class="row g-2 mb-2">
      <div class="col-12"><input type="text" name="q" class="form-control" placeholder="Search notes / category / source" value="{{ q }}"></div>
      <div class="col-6">
        <select name="type" class="form-select">
          <option value="" {{ 'selected' if type_f=='' }}>All Types</option>
          <option value="allowance" {{ 'selected' if type_f=='allowance' }}>Allowance</option>
          <option value="expense" {{ 'selected' if type_f=='expense' }}>Expense</option>
          <option value="savings" {{ 'selected' if type_f=='savings' }}>Savings</option>
        </select>
      </div>
      <div class="col-6">
        <select name="sort" class="form-select">
          <option value="date_desc" {{ 'selected' if sort=='date_desc' }}>Newest first</option>
          <option value="date_asc" {{ 'selected' if sort=='date_asc' }}>Oldest first</option>
          <option value="amount_desc" {{ 'selected' if sort=='amount_desc' }}>Amount: High-Low</option>
          <option value="amount_asc" {{ 'selected' if sort=='amount_asc' }}>Amount: Low-High</option>
        </select>
      </div>
      <div class="col-12"><button class="btn-ghost w-100 py-2" data-no-spin="1"><i class="bi bi-funnel"></i> Apply Filters</button></div>
    </form>
    <div class="d-flex gap-2 mb-2">
      <a href="{{ url_for('export_csv') }}" class="btn-ghost flex-fill text-center py-2" style="font-size:.82rem;"><i class="bi bi-file-earmark-excel"></i> Export Excel</a>
      <a href="{{ url_for('export_print') }}" target="_blank" class="btn-ghost flex-fill text-center py-2" style="font-size:.82rem;"><i class="bi bi-file-earmark-pdf"></i> Export PDF</a>
    </div>
  </div>

  <div class="glass card-app">
    {% if rows %}
      {% for t in rows %}
      <div class="list-tx">
        <div class="tx-icon" style="background:{{ 'rgba(34,197,94,.15)' if t.type=='allowance' else ('rgba(244,63,94,.15)' if t.type=='expense' else 'rgba(59,130,246,.15)') }};">
          <i class="bi {{ 'bi-arrow-down-left' if t.type=='allowance' else ('bi-cart3' if t.type=='expense' else 'bi-piggy-bank') }}"
             style="color:{{ 'var(--success)' if t.type=='allowance' else ('var(--danger)' if t.type=='expense' else 'var(--accent-1)') }}"></i>
        </div>
        <div class="flex-grow-1">
          <div class="fw-semibold" style="font-size:.88rem;">{{ t.category or t.type|capitalize }}</div>
          <div class="text-lo" style="font-size:.72rem;">{{ t.date }} {% if t.notes %}· {{ t.notes[:28] }}{% endif %}</div>
        </div>
        <div class="text-end">
          <div class="fw-bold" style="font-size:.9rem; color:{{ 'var(--success)' if t.type=='allowance' else 'var(--danger)' }}">
            {{ '+' if t.type=='allowance' else '-' }}₱{{ '%.2f'|format(t.amount) }}
          </div>
          <div class="d-flex gap-2 justify-content-end mt-1">
            <a href="{{ url_for('edit_transaction', tx_id=t.id) }}" class="text-lo" style="font-size:.8rem;"><i class="bi bi-pencil-square"></i></a>
            <form method="POST" action="{{ url_for('delete_transaction', tx_id=t.id) }}" onsubmit="return confirm('Delete this transaction?');" style="display:inline;">
              <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
              <button type="submit" class="btn btn-sm p-0 border-0 bg-transparent text-danger" data-no-spin="1" style="font-size:.8rem;"><i class="bi bi-trash3"></i></button>
            </form>
          </div>
        </div>
      </div>
      {% endfor %}
      <nav class="mt-3">
        <ul class="pagination pagination-sm justify-content-center mb-0">
          {% for p in range(1, total_pages+1) %}
          <li class="page-item {{ 'active' if p==page }}">
            <a class="page-link" style="background:var(--glass); border-color:var(--glass-brd); color:var(--text-hi);"
               href="{{ url_for('transactions', page=p, q=q, type=type_f, sort=sort) }}">{{ p }}</a>
          </li>
          {% endfor %}
        </ul>
      </nav>
    {% else %}
      <div class="text-center text-lo py-4"><i class="bi bi-search" style="font-size:2rem;"></i><p class="mb-0 mt-2">No transactions found.</p></div>
    {% endif %}
  </div>
""" + APP_SHELL_TAIL

# --- EDIT TRANSACTION ----------------------------------------------------------------------
EDIT_TX_HTML = """
{% extends "base.html" %}
{% block title %}Edit Transaction{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <h5 class="fw-bold mb-3"><i class="bi bi-pencil-square"></i> Edit {{ t.type|capitalize }}</h5>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">AMOUNT (₱)</label>
        <input type="number" step="0.01" min="0.01" name="amount" class="form-control" value="{{ t.amount }}" required>
      </div>
      <div class="mb-3">
        <label class="form-label">{{ 'CATEGORY' if t.type=='expense' else 'SOURCE / LABEL' }}</label>
        {% if t.type == 'expense' %}
        <select name="category" class="form-select">
          {% for c in categories %}<option value="{{ c }}" {{ 'selected' if c==t.category }}>{{ c }}</option>{% endfor %}
        </select>
        {% else %}
        <input type="text" name="category" class="form-control" value="{{ t.category or '' }}">
        {% endif %}
      </div>
      <div class="mb-3">
        <label class="form-label">DATE</label>
        <input type="date" name="date" class="form-control" value="{{ t.date }}" required>
      </div>
      <div class="mb-3">
        <label class="form-label">NOTES</label>
        <textarea name="notes" class="form-control" rows="2" maxlength="300">{{ t.notes or '' }}</textarea>
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Updating...">Update Transaction</button>
    </form>
  </div>
""" + APP_SHELL_TAIL

# --- BUDGET ----------------------------------------------------------------------------------
BUDGET_HTML = """
{% extends "base.html" %}
{% block title %}Budget{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <div class="d-flex justify-content-between mb-2">
      <span class="section-title mb-0">{{ bstats.budget_type|capitalize }} Budget</span>
      <span class="pill {{ 'pill-danger' if bstats.budget_pct >= 100 else ('pill-warning' if bstats.budget_pct >= 80 else 'pill-success') }}">
        {{ bstats.budget_pct }}% used
      </span>
    </div>
    <div class="progress mb-2"><div class="progress-bar" style="width:{{ [bstats.budget_pct, 100]|min }}%; {{ 'background:linear-gradient(90deg,#f43f5e,#f97316);' if bstats.budget_pct>=100 else ('background:linear-gradient(90deg,#f59e0b,#fbbf24);' if bstats.budget_pct>=80 else '') }}"></div></div>
    <div class="d-flex justify-content-between text-lo" style="font-size:.78rem;">
      <span>Spent: ₱{{ '%.2f'|format(bstats.period_expense) }}</span>
      <span>Budget: ₱{{ '%.2f'|format(bstats.budget_amount) }}</span>
    </div>
    {% if bstats.budget_pct >= 80 %}
    <div class="mt-2 pill {{ 'pill-danger' if bstats.budget_pct>=100 else 'pill-warning' }}" style="display:inline-block;">
      <i class="bi bi-exclamation-triangle-fill"></i> {{ 'Budget exceeded!' if bstats.budget_pct>=100 else 'Approaching your limit' }}
    </div>
    {% endif %}
  </div>

  <div class="glass card-app">
    <div class="section-title">Set Budget</div>
    <form method="POST">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3">
        <label class="form-label">BUDGET TYPE</label>
        <select name="budget_type" class="form-control" onchange="window.location.href='{{ url_for('budget') }}?type=' + encodeURIComponent(this.value)">
          <option value="yearly" {{ 'selected' if bstats.budget_type=='yearly' else '' }}>Yearly</option>
          <option value="monthly" {{ 'selected' if bstats.budget_type=='monthly' else '' }}>Monthly</option>
          <option value="weekly" {{ 'selected' if bstats.budget_type=='weekly' else '' }}>Weekly</option>
          <option value="daily" {{ 'selected' if bstats.budget_type=='daily' else '' }}>Daily</option>
        </select>
      </div>
      <div class="mb-3">
        <label class="form-label">BUDGET AMOUNT (₱)</label>
        <input type="number" step="0.01" min="0" name="budget_amount" class="form-control" value="{{ bstats.budget_amount }}">
      </div>
      <button type="submit" class="grad-btn w-100" data-loading="Saving...">Save Budget</button>
    </form>
  </div>

  <div class="row g-2">
    <div class="col-6">
      <div class="glass card-app mb-0">
        <div class="text-lo" style="font-size:.72rem;">{{ bstats.budget_type|capitalize }} Remaining</div>
        <div class="stat-value" style="font-size:1.1rem;">₱{{ '%.2f'|format(bstats.budget_remaining) }}</div>
      </div>
    </div>
    <div class="col-6">
      <div class="glass card-app mb-0">
        <div class="text-lo" style="font-size:.72rem;">Expenses This Period</div>
        <div class="stat-value" style="font-size:1.1rem;">{{ bstats.period_count }}</div>
      </div>
    </div>
  </div>
""" + APP_SHELL_TAIL

# --- SAVINGS ----------------------------------------------------------------------------------
SAVINGS_HTML = """
{% extends "base.html" %}
{% block title %}Savings{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <div class="section-title">Add Savings Goal</div>
    <form method="POST" action="{{ url_for('add_goal') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="row g-2">
        <div class="col-12"><input type="text" name="goal_name" class="form-control" placeholder="Goal name (e.g. New Phone)" required maxlength="60"></div>
        <div class="col-6"><input type="number" step="0.01" min="1" name="goal_amount" class="form-control" placeholder="Target ₱" required></div>
        <div class="col-6"><input type="date" name="deadline" class="form-control"></div>
        <div class="col-12"><button class="grad-btn w-100" data-loading="Creating...">Create Goal</button></div>
      </div>
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title">Transfer to Savings</div>
    <form method="POST" action="{{ url_for('add_savings_tx') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="row g-2">
        <div class="col-6"><input type="number" step="0.01" min="0.01" name="amount" class="form-control" placeholder="Amount ₱" required></div>
        <div class="col-6">
          <select name="goal_id" class="form-select">
            <option value="">General Savings</option>
            {% for gl in goals %}<option value="{{ gl.id }}">{{ gl.goal_name }}</option>{% endfor %}
          </select>
        </div>
        <div class="col-12"><button class="btn-ghost w-100 py-2" data-loading="Transferring...">Transfer Funds</button></div>
      </div>
    </form>
  </div>

  <div class="section-title">Your Goals</div>
  {% if goals %}
    <div class="row g-2">
    {% for gl in goals %}
      {% set pct = (gl.current_saved / gl.goal_amount * 100) if gl.goal_amount else 0 %}
      {% set pct = [pct, 100]|min %}
      <div class="col-6">
        <div class="glass card-app text-center mb-0">
          <svg width="86" height="86" viewBox="0 0 86 86" class="progress-ring mx-auto">
            <circle cx="43" cy="43" r="36" stroke="rgba(255,255,255,.12)" stroke-width="8" fill="none"/>
            <circle cx="43" cy="43" r="36" stroke="url(#grad)" stroke-width="8" fill="none"
              stroke-dasharray="{{ 226.19 }}" stroke-dashoffset="{{ 226.19 - (226.19*pct/100) }}" stroke-linecap="round" transform="rotate(-90 43 43)"/>
            <defs><linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#3b82f6"/><stop offset="100%" stop-color="#22d3ee"/>
            </linearGradient></defs>
            <text x="43" y="48" text-anchor="middle" fill="var(--text-hi)" font-size="16" font-weight="700">{{ pct|round|int }}%</text>
          </svg>
          <div class="fw-semibold mt-1" style="font-size:.85rem;">{{ gl.goal_name }}</div>
          <div class="text-lo" style="font-size:.72rem;">₱{{ '%.0f'|format(gl.current_saved) }} / ₱{{ '%.0f'|format(gl.goal_amount) }}</div>
          {% if gl.deadline %}<div class="text-lo" style="font-size:.68rem;"><i class="bi bi-calendar-event"></i> {{ gl.deadline }}</div>{% endif %}
        </div>
      </div>
    {% endfor %}
    </div>
  {% else %}
    <div class="glass card-app text-center text-lo py-4"><i class="bi bi-piggy-bank" style="font-size:2rem;"></i><p class="mb-0 mt-2">No savings goals yet.</p></div>
  {% endif %}
""" + APP_SHELL_TAIL

# --- REPORTS ----------------------------------------------------------------------------------
REPORTS_HTML = """
{% extends "base.html" %}
{% block title %}Reports{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="d-flex gap-2 mb-2">
    <a href="{{ url_for('export_csv') }}" class="btn-ghost flex-fill text-center py-2" style="font-size:.82rem;"><i class="bi bi-file-earmark-excel"></i> Export Excel</a>
    <a href="{{ url_for('export_print') }}" target="_blank" class="btn-ghost flex-fill text-center py-2" style="font-size:.82rem;"><i class="bi bi-printer"></i> Print / PDF</a>
  </div>

  <div class="glass card-app"><div class="section-title">Monthly Allowance vs Expenses</div><canvas id="chartMonthly" height="180"></canvas></div>
  <div class="glass card-app"><div class="section-title">Category Breakdown</div><canvas id="chartCategory" height="200"></canvas></div>
  <div class="glass card-app"><div class="section-title">Savings Growth</div><canvas id="chartSavings" height="160"></canvas></div>
  <div class="glass card-app"><div class="section-title">Budget Usage</div><canvas id="chartBudget" height="160"></canvas></div>
  <div class="glass card-app"><div class="section-title">Weekly Spending</div><canvas id="chartWeekly" height="160"></canvas></div>
  <div class="glass card-app"><div class="section-title">Yearly Summary ({{ year }})</div><canvas id="chartYearly" height="180"></canvas></div>
""" + APP_SHELL_TAIL + """
{% block extra_scripts %}
<script>
const gridColor = 'rgba(255,255,255,.08)', textColor = getComputedStyle(document.documentElement).getPropertyValue('--text-lo');
Chart.defaults.color = textColor.trim() || '#9fb3d1';
Chart.defaults.borderColor = gridColor;
const cd = {{ cd|tojson }};

new Chart(document.getElementById('chartMonthly'), {type:'bar', data:{labels:cd.months, datasets:[
  {label:'Allowance', data:cd.allowance_series, backgroundColor:'#22c55e', borderRadius:6},
  {label:'Expenses', data:cd.expense_series, backgroundColor:'#f43f5e', borderRadius:6}
]}, options:{responsive:true, plugins:{legend:{labels:{boxWidth:10}}}}});

new Chart(document.getElementById('chartCategory'), {type:'doughnut', data:{labels:cd.cat_labels, datasets:[{
  data:cd.cat_values, backgroundColor:['#3b82f6','#22d3ee','#6366f1','#f59e0b','#f43f5e','#22c55e','#a855f7']
}]}, options:{responsive:true, plugins:{legend:{position:'bottom', labels:{boxWidth:10, font:{size:10}}}}}});

new Chart(document.getElementById('chartSavings'), {type:'line', data:{labels:cd.months, datasets:[{
  label:'Cumulative Savings', data:cd.savings_growth, borderColor:'#22d3ee', backgroundColor:'rgba(34,211,238,.15)', fill:true, tension:.35
}]}, options:{responsive:true}});

new Chart(document.getElementById('chartBudget'), {type:'doughnut', data:{labels:['Used','Remaining'], datasets:[{
  data:[cd.budget_used, cd.budget_free], backgroundColor:['#f59e0b','rgba(255,255,255,.10)']
}]}, options:{responsive:true, cutout:'70%'}});

new Chart(document.getElementById('chartWeekly'), {type:'bar', data:{labels:cd.week_labels, datasets:[{
  label:'Spending', data:cd.week_values, backgroundColor:'#6366f1', borderRadius:6
}]}, options:{responsive:true, plugins:{legend:{display:false}}}});

new Chart(document.getElementById('chartYearly'), {type:'bar', data:{labels:cd.year_labels, datasets:[{
  label:'Expenses', data:cd.year_values, backgroundColor:'#3b82f6', borderRadius:6
}]}, options:{responsive:true, plugins:{legend:{display:false}}}});
</script>
{% endblock %}
"""

# --- SETTINGS ----------------------------------------------------------------------------------
SETTINGS_HTML = """
{% extends "base.html" %}
{% block title %}Settings{% endblock %}
""" + APP_SHELL_HEAD + """
  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-person-circle"></i> Profile Photo</div>
    <div class="d-flex align-items-center gap-3 mb-3">
      {% if user.profile_pic %}<img src="{{ user.profile_pic }}" class="avatar" style="width:64px;height:64px;">
      {% else %}<div class="avatar" style="width:64px;height:64px;font-size:1.4rem;">{{ user.full_name[0]|upper }}</div>{% endif %}
      <div>
        <div class="text-lo" style="font-size:.72rem;">@{{ user.username }} · Joined {{ user.date_joined[:10] }}</div>
        {% if user.bio %}
          <div class="text-hi mt-1" style="font-size:.8rem;">{{ user.bio }}</div>
        {% else %}
          <div class="text-lo mt-1" style="font-size:.76rem;">Current profile photo</div>
        {% endif %}
      </div>
    </div>
    <form method="POST" action="{{ url_for('update_picture') }}" enctype="multipart/form-data" id="profilePicForm">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="d-flex gap-2">
        <label class="btn-ghost flex-fill py-2 mb-0 text-center" style="font-size:.78rem; cursor:pointer;">
          <i class="bi bi-upload"></i> Change Profile Photo
          <input type="file" name="profile_pic" accept="image/*" hidden onchange="submitPhotoForm(this)">
        </label>
        {% if user.profile_pic %}
        <button type="submit" name="remove" value="1" class="btn-ghost flex-fill py-2" style="font-size:.78rem;" formnovalidate>
          <i class="bi bi-trash3"></i> Remove Profile Photo
        </button>
        {% endif %}
      </div>
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-image-fill"></i> Cover Photo</div>
    {% if user.cover_photo %}
      <img src="{{ user.cover_photo }}" class="cover-photo-preview mb-3">
    {% else %}
      <div class="cover-photo-preview mb-3 d-flex align-items-center justify-content-center text-lo" style="font-size:.75rem;">No cover photo set</div>
    {% endif %}
    <form method="POST" action="{{ url_for('update_cover_photo') }}" enctype="multipart/form-data" id="coverPhotoForm">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="d-flex gap-2">
        <label class="btn-ghost flex-fill py-2 mb-0 text-center" style="font-size:.78rem; cursor:pointer;">
          <i class="bi bi-upload"></i> Change Cover Photo
          <input type="file" name="cover_photo" accept="image/*" hidden onchange="submitPhotoForm(this)">
        </label>
        {% if user.cover_photo %}
        <button type="submit" name="remove" value="1" class="btn-ghost flex-fill py-2" style="font-size:.78rem;" formnovalidate>
          <i class="bi bi-trash3"></i> Remove Cover Photo
        </button>
        {% endif %}
      </div>
    </form>
  </div>

  <script>
  function submitPhotoForm(input){
    if(!input.files || !input.files[0]) return;
    var form = input.closest('form');
    var label = input.parentElement;
    label.style.position = 'relative';
    var spin = document.createElement('div');
    spin.className = 'upload-spinner-overlay';
    spin.innerHTML = '<div class="upload-spinner"></div>';
    label.appendChild(spin);
    form.submit();
  }
  </script>

  <div class="row g-2 mb-2">
    <div class="col-6"><div class="glass card-app mb-0 text-center"><div class="stat-value" style="font-size:1rem;">{{ stats.total_allowance + stats.total_expense }}</div><div class="text-lo" style="font-size:.7rem;">Transactions Value</div></div></div>
    <div class="col-6"><div class="glass card-app mb-0 text-center"><div class="stat-value" style="font-size:1rem;">₱{{ '%.0f'|format(stats.total_savings) }}</div><div class="text-lo" style="font-size:.7rem;">Total Savings</div></div></div>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-person-lines-fill"></i> Profile</div>
    <form method="POST" action="{{ url_for('update_profile') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-3"><label class="form-label">FULL NAME</label><input type="text" name="full_name" class="form-control" value="{{ user.full_name }}" required></div>
      <div class="mb-3"><label class="form-label">USERNAME</label><input type="text" name="username" class="form-control" value="{{ user.username }}" required></div>
      <div class="mb-3"><label class="form-label">BIO</label><textarea name="bio" class="form-control" rows="2" placeholder="Tell us a bit about yourself" maxlength="150">{{ user.bio or '' }}</textarea></div>
      <button type="submit" class="btn-ghost w-100 py-2" data-loading="Saving...">Save Profile</button>
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-shield-lock-fill"></i> Security</div>
    <form method="POST" action="{{ url_for('change_password') }}" class="mb-3">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="mb-2"><input type="password" name="current_password" class="form-control" placeholder="Current password" required></div>
      <div class="mb-2"><input type="password" name="new_password" class="form-control" placeholder="New password" required></div>
      <div class="mb-2"><input type="password" name="confirm_password" class="form-control" placeholder="Confirm new password" required></div>
      <button type="submit" class="btn-ghost w-100 py-2" data-loading="Updating...">Change Password</button>
    </form>
    <form method="POST" action="{{ url_for('update_session_timeout') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label class="form-label">SESSION TIMEOUT</label>
      <div class="d-flex gap-2">
        <select name="session_timeout" class="form-select" onchange="this.form.submit()">
          <option value="15" {{ 'selected' if user.session_timeout==15 }}>15 minutes</option>
          <option value="30" {{ 'selected' if user.session_timeout==30 }}>30 minutes</option>
          <option value="60" {{ 'selected' if user.session_timeout==60 }}>1 hour</option>
          <option value="240" {{ 'selected' if user.session_timeout==240 }}>4 hours</option>
        </select>
      </div>
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-palette-fill"></i> Appearance</div>
    <form method="POST" action="{{ url_for('update_theme') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="row g-2">
        {% for th, label, icon in [('light','Light','bi-sun-fill'),('dark','Dark','bi-moon-stars-fill'),('blue','Blue','bi-droplet-fill'),('system','System','bi-phone-fill')] %}
        <div class="col-3">
          <button type="submit" name="theme" value="{{ th }}" class="btn-ghost w-100 py-3" data-no-spin="1" style="{{ 'border-color:var(--accent-2); box-shadow:0 0 0 2px rgba(34,211,238,.3);' if user.theme==th }}">
            <i class="bi {{ icon }} d-block mb-1"></i><span style="font-size:.7rem;">{{ label }}</span>
          </button>
        </div>
        {% endfor %}
      </div>
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-bell-fill"></i> Notifications</div>
    <form method="POST" action="{{ url_for('update_notifications') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      {% for key, label in [('notifications_enabled','Enable Notifications'),('budget_alerts','Budget Alerts'),('savings_alerts','Savings Alerts')] %}
      <div class="form-check form-switch d-flex justify-content-between align-items-center py-2">
        <label class="form-check-label" for="{{ key }}">{{ label }}</label>
        <input class="form-check-input" type="checkbox" role="switch" id="{{ key }}" name="{{ key }}" {{ 'checked' if user[key] }} onchange="this.form.submit()">
      </div>
      {% endfor %}
    </form>
  </div>

  <div class="glass card-app">
    <div class="section-title"><i class="bi bi-info-circle-fill"></i> About</div>
    <div class="d-flex justify-content-between py-1"><span class="text-lo">App Name</span><span class="fw-semibold">{{ app_name }}</span></div>
    <div class="d-flex justify-content-between py-1"><span class="text-lo">Version</span><span class="fw-semibold">{{ app_version }}</span></div>
    <div class="d-flex justify-content-between py-1"><span class="text-lo">Developer</span><span class="fw-semibold">{{ developer }}</span></div>
  </div>

  <form method="POST" action="{{ url_for('logout') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit" class="w-100 py-2 mb-3" style="background:rgba(244,63,94,.15); color:var(--danger); border:1px solid rgba(244,63,94,.3); border-radius:16px; font-weight:700;" data-loading="Logging out...">
      <i class="bi bi-box-arrow-right"></i> Logout
    </button>
  </form>
""" + APP_SHELL_TAIL

# --- PRINT / PDF-STYLE EXPORT -------------------------------------------------------------
PRINT_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{{ app_name }} — Report</title>
<style>
body{font-family:Arial,sans-serif; padding:24px; color:#0b1d3a;}
h1{margin-bottom:0;} .sub{color:#666; margin-top:2px;}
table{width:100%; border-collapse:collapse; margin-top:16px;}
th,td{border:1px solid #ddd; padding:8px; font-size:13px; text-align:left;}
th{background:#eef3fb;}
.pos{color:#16a34a; font-weight:700;} .neg{color:#e11d48; font-weight:700;}
.summary{display:flex; gap:24px; margin:16px 0;}
.summary div{border:1px solid #ddd; border-radius:8px; padding:10px 16px;}
@media print{ .noprint{display:none;} }
</style></head>
<body>
<button class="noprint" onclick="window.print()" style="padding:10px 18px;border-radius:8px;border:none;background:#3b82f6;color:#fff;font-weight:700;margin-bottom:12px;">Print / Save as PDF</button>
<h1>{{ app_name }}</h1>
<div class="sub">Transaction Report for {{ user.full_name }} (@{{ user.username }}) — generated {{ now }}</div>
<div class="summary">
  <div>Total Allowance<br><b>₱{{ '%.2f'|format(stats.total_allowance) }}</b></div>
  <div>Total Expenses<br><b>₱{{ '%.2f'|format(stats.total_expense) }}</b></div>
  <div>Balance<br><b>₱{{ '%.2f'|format(stats.balance) }}</b></div>
  <div>Total Savings<br><b>₱{{ '%.2f'|format(stats.total_savings) }}</b></div>
</div>
<table>
<thead><tr><th>Date</th><th>Type</th><th>Category / Source</th><th>Notes</th><th>Amount</th></tr></thead>
<tbody>
{% for t in rows %}
<tr>
  <td>{{ t.date }}</td><td>{{ t.type|capitalize }}</td><td>{{ t.category or '-' }}</td><td>{{ t.notes or '-' }}</td>
  <td class="{{ 'pos' if t.type=='allowance' else 'neg' }}">{{ '+' if t.type=='allowance' else '-' }}₱{{ '%.2f'|format(t.amount) }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</body></html>
"""

# --- register all templates with Jinja's DictLoader ----------------------------------------
app.jinja_loader = DictLoader({
    "base.html": BASE_HTML,
    "login.html": LOGIN_HTML,
    "register.html": REGISTER_HTML,
    "verify.html": VERIFY_HTML,
    "forgot.html": FORGOT_HTML,
    "dashboard.html": DASHBOARD_HTML,
    "add_allowance.html": ADD_ALLOWANCE_HTML,
    "add_expense.html": ADD_EXPENSE_HTML,
    "transactions.html": TRANSACTIONS_HTML,
    "edit_tx.html": EDIT_TX_HTML,
    "budget.html": BUDGET_HTML,
    "savings.html": SAVINGS_HTML,
    "reports.html": REPORTS_HTML,
    "settings.html": SETTINGS_HTML,
    "print.html": PRINT_HTML,
})


@app.context_processor
def inject_globals():
    return {
        "app_name": APP_NAME, "app_tagline": APP_TAGLINE, "app_version": APP_VERSION,
        "developer": DEVELOPER, "csrf_token": csrf_token, "user": current_user(),
        "categories": CATEGORIES, "sources": SOURCES_HINT,
    }


# =====================================================================================
# SECTION 6B — PWA / APK SUPPORT
# =====================================================================================
# Installable Progressive Web App support. This lets the app be installed as an icon on
# a phone's home screen, and — combined with a free tool like https://www.pwabuilder.com
# (paste the live URL in, no coding needed) — lets you generate a real downloadable .apk
# that wraps this same live app. The icons below are generated once and embedded as
# base64 so no separate image files or static/ folder are needed.
_ICON_192_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAHGUlEQVR4nO3dzW9UVRjH8XOnd9pCKeUlEfoCGqzWhVEX+AeIBk1MXItxKa0rlybyBxB3xkTUGKNudOHOPZQYE010g12YQIzIvAKJnZcyLbTz4uI2Y+3bvDz3znnOvd9P7oIZ2sthzq/nPPeeM1Nv8v0lA/QrZbsBcBsBgohvPNtNgMt8jwRBgCkMIgQIItRAEPHJDyR8wxAEAWogiFADQYQRCCIU0RBhBIKIbzzGIPSPEQgi1EAQYQSCCPeBIMJSBkSYwiBCEQ0RaiCIMIVBhCIaIoxAEKGIhghFNESYwiBCgCDie+wHggAjEEQIEEQIEEQIEER8amhIsBYGEaYwiBAgiFADQYQRCCIU0RBhOwdEmMIgQoAgwpZWiFBEQ4QiGiLUQBChBoIIIxBEqIEgwlUYRJjCIEIRDRFqIIgwhUGEIhoi1EAQoQaCCDUQRAgQRAgQRPiAKYj4thvQp+vvzthuQvhe+jxnuwk98+Yu/2W7Db2JZXS2citGLgUo9tHZypUYpYxnnDgSlR4T/LTYfs27OVK2G9DVcX0hWekJXF+Ysf7KdzxYC1NOe+84cB9ocWHadhOs0f9/Zy3Mssk/n9v5ZHF2qf3nxYXpc1/kB9ii3njPfHjbdhv2sziv/UewP7vmZqd2ktRmiO0cg9ZldLZ+cXF2KfhBellfjFK2q/iOR6z0lJ6d33Vtftp2d2w/HCiiY6O/9Mi/N1LqbyTGhTwBwRmuzU/Z75Qth/YbifEQ1vixmaGLU9b7pX24uhofA/X06dqRt7c+47UaXnMlvX5rpPaT13pkq2E98Q37gSK21/DT9E+sTry561+lGstH7l0aWf1517MVZ5euXpx65ctimK3sF0W0Rs2hY6WTH9XTp203pDOmMDuaqcP14TPth+PLnw1tZFupsbXx19dHXzDGtFIHHhybHyt/l370h7VWdoEiOlp7zV/1kadqExfaD9cOna9NXFgdf6NlhrzmavDkw0PnV46/t9c5r74zab13KKKtGarfH177tT0Ijaz+MlQvBH+uTbzVSB00xniNldHaNWtN7A6LqXY0vQNN/7H2w4Z/ouX5xpiNkWcb6c3NT6lmpeHvuxSooO/YD2RHev3WWPnbh2Pngof14TNe82QrNVYffqL9NeP/fDxa+3Hf09jvO+1T2OTRtO0m9KlY2uj+i+vDT2575mDl+07pUYHVeBW85qoxTc80vcaD9PrNA9UfRmuLnb9rAC3rhBpIheOF+fTDpc5ft42CvuNGYlSCyXfr3sKwBOc8/9Xd0M/cBwIEEd/TMA6id0o6jv1AEYpiFgvO9urX9+x3jWeMp/4yPsaG135Tu8+we9RA0Qp3EPpv+FGjtxGoVgy/6WOTJ0I/pyqTR9PF0kZxdkk43ihMj9n5AVMPivteHEZQlNTu7v+KxOdd8ZIMtccwbR8I5ndIDMIQDEKm3wy10/PaN/dDbpkYRfSAbBZDpY0gDb2+M1VhdAIEaKC2DkWbz3R6b7xRnB5jjK9sSo2/9lAUPNz/Ai34Ys19xH4gO9rbVHbd9bFjE4vePmIKs8zdDU8BbiRChBHIBXpnMHYkukBzHzGFQYSrMCfo7SP2RLtAcR8xhUGEqzAHKB6AGIEgQw3kAsWLYYxAECFAEOFOtAM09xFXYS5QnCCmMIiwlOEEvX2k/UM25z64YfslsuzsJ/es98I+B1MYRNR/uIJn5i7dsP0qWXP2ipYPUdjrYATS6+wVXe9i3pX+XzjnGePNXfrd9gtlhf1XvuPhPe1OlXrz8vO2mzAgL36q952E27gUoEDsY+RQeoyLAQrEMkZuRSfgaoDi6vDMpO0m9Ib9QLpU80W3MsRqvDorueLhmSnbregW94E0quYKtpvQLQKkVDVXcCJGBEg1/RmiiNaumi9oLon4VQcO0FxWM4W5Qe1cRoCcoTNDBMgl1VzB9ur79oMAOaaaLdjOzP8OAuSeSlbRXMa7MpxUyRYnTqm4LuM+kKsquYKGDDGFOUzDXMZqvNuqWcvjECOQ8+yOQ9RAcWCxHuLTOWLDzkjAFBYTlWyeG4kQKWfzg/9HCVCslLN51sLgEpYy4qacKRw5PT2wf44biTFUyeQHliHuA8VTOTugDFEDQYQAxVY5M4iregIUZ+VMPurreF/x7/FACKLuX0agmCtncpGenwBBhADFX6SDEHeiE6GcyR99fCaKMzMCQYQAJUXpTiQTGQGCCGthCVLK5EKvhFiNT5bQu5ursGQp3Qn5cowaCCIECCIU0YkTbilNEZ1EIXY6U1gShXhTkbc2J1VIG4UYgSBCgBKq9Hc2lPMwhSVXKHMYIxBE/Mh3XUOr5Tu5Y0+cEp6EEQgi3EhMNHnvMwIl2rL4Woy1sMSTBYARCCJsKIMoABTRSSe8jcMUlnTLt0V1NAGCCAGCCIupMJJf/c59IIiuw/4F1AaRLOQSDBgAAAAASUVORK5CYII="
)
_ICON_512_B64 = (
    "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"
)
_ICON_APPLE_TOUCH_B64 = (
    "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"
)


@app.route("/manifest.json")
def manifest():
    manifest_data = {
        "id": "/",
        "name": APP_NAME,
        "short_name": APP_NAME,
        "description": APP_TAGLINE,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#060f24",
        "theme_color": "#0b1d3a",
        "orientation": "portrait",
        "categories": ["finance", "productivity", "lifestyle"],
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(manifest_data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    # Service worker with real caching logic (required by PWABuilder's "add caching" check):
    #   - Static assets (icons, manifest) are cache-first for speed on repeat visits/offline.
    #   - Everything else (dashboard, login, API calls, etc.) is network-first, since this
    #     app is session/DB-driven and must never serve stale financial data. The cache is
    #     only used as a fallback if the network request fails (e.g. brief connectivity drop).
    js = (
        "const CACHE_NAME = 'allowance-static-v1';\n"
        "const STATIC_ASSETS = ['/icon-192.png', '/icon-512.png', '/apple-touch-icon.png', '/manifest.json'];\n"
        "\n"
        "self.addEventListener('install', e => {\n"
        "  self.skipWaiting();\n"
        "  e.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS)).catch(() => {}));\n"
        "});\n"
        "\n"
        "self.addEventListener('activate', e => {\n"
        "  e.waitUntil(\n"
        "    caches.keys().then(names => Promise.all(\n"
        "      names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n))\n"
        "    )).then(() => self.clients.claim())\n"
        "  );\n"
        "});\n"
        "\n"
        "self.addEventListener('fetch', e => {\n"
        "  const url = new URL(e.request.url);\n"
        "  if (e.request.method !== 'GET') return;\n"
        "\n"
        "  if (STATIC_ASSETS.includes(url.pathname)) {\n"
        "    // Cache-first for static assets\n"
        "    e.respondWith(\n"
        "      caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {\n"
        "        const clone = res.clone();\n"
        "        caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));\n"
        "        return res;\n"
        "      }))\n"
        "    );\n"
        "  } else {\n"
        "    // Network-first for everything else (live session/DB data)\n"
        "    e.respondWith(\n"
        "      fetch(e.request).catch(() => caches.match(e.request))\n"
        "    );\n"
        "  }\n"
        "});\n"
    )
    return Response(js, mimetype="application/javascript")


@app.route("/icon-192.png")
def icon_192():
    import base64
    return Response(base64.b64decode(_ICON_192_B64), mimetype="image/png")


@app.route("/icon-512.png")
def icon_512():
    import base64
    return Response(base64.b64decode(_ICON_512_B64), mimetype="image/png")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    import base64
    return Response(base64.b64decode(_ICON_APPLE_TOUCH_B64), mimetype="image/png")


# =====================================================================================
# SECTION 7 — AUTH ROUTES
# =====================================================================================
@app.route("/google09494044a63fb0be.html")
def google_site_verification():
    return Response("google-site-verification: google09494044a63fb0be.html", mimetype="text/html")


@app.route("/")
def root():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = sanitize(request.form.get("full_name"), 80)
        username = sanitize(request.form.get("username"), 20)
        email = sanitize(request.form.get("email"), 120).lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not valid_username(username):
            errors.append("Username must be 3-20 characters (letters, numbers, _ or .).")
        if not valid_email(email):
            errors.append("Please provide a valid email address.")
        score, missing = password_strength(password)
        if score < 5:
            errors.append("Password does not meet all strength requirements.")
        if password != confirm:
            errors.append("Passwords do not match.")

        db = get_db()
        if not errors:
            exists = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if exists:
                errors.append("Username is already taken.")
            email_exists = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if email_exists:
                errors.append("An account with that email already exists.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html")

        cur = db.execute(
            "INSERT INTO users (full_name, username, email, password_hash, date_joined, monthly_budget, weekly_budget, daily_budget) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
            (full_name, username, email, generate_password_hash(password), datetime.now().isoformat())
        )
        db.commit()
        user_id = cur.lastrowid

        code, sent_ok = issue_verification_code(db, user_id, email, full_name)
        session["_pending_verify_user_id"] = user_id
        if sent_ok:
            flash("Account created! Please check your email for a verification code.", "success")
        else:
            flash("Account created, but the verification email could not be sent. "
                  "Check BREVO_API_KEY / EMAIL_FROM in your .env, or use Resend Code.", "warning")
        return redirect(url_for("verify_email"))

    return render_template("register.html")


@app.route("/api/check-username")
def api_check_username():
    username = sanitize(request.args.get("username", ""), 20)
    valid = valid_username(username)
    available = True
    if valid:
        db = get_db()
        row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        available = row is None
    return jsonify({"valid": valid, "available": available})


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    user_id = session.get("_pending_verify_user_id")
    if not user_id:
        flash("Please register or log in first.", "info")
        return redirect(url_for("register"))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        session.pop("_pending_verify_user_id", None)
        return redirect(url_for("register"))

    if user["email_verified"]:
        session.pop("_pending_verify_user_id", None)
        flash("Email already verified. Please sign in.", "success")
        return redirect(url_for("login"))

    if request.method == "POST":
        code = sanitize(request.form.get("code"), 6)
        expiry = user["verification_expiry"]
        expired = (not expiry) or (datetime.now() > datetime.fromisoformat(expiry))

        if expired:
            flash("Your verification code has expired. Please request a new one.", "danger")
        elif not code or code != user["verification_code"]:
            flash("Invalid verification code.", "danger")
        else:
            db.execute(
                "UPDATE users SET email_verified=1, verification_code=NULL, verification_expiry=NULL WHERE id=?",
                (user_id,)
            )
            db.commit()
            session.pop("_pending_verify_user_id", None)
            flash("Email verified! You can now sign in.", "success")
            return redirect(url_for("login"))

    return render_template("verify.html", pending_email=user["email"])


@app.route("/resend-code", methods=["POST"])
def resend_code():
    user_id = session.get("_pending_verify_user_id")
    if not user_id:
        flash("Please register or log in first.", "info")
        return redirect(url_for("register"))

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        session.pop("_pending_verify_user_id", None)
        return redirect(url_for("register"))

    code, sent_ok = issue_verification_code(db, user_id, user["email"], user["full_name"])
    if sent_ok:
        flash("A new verification code has been sent to your email.", "success")
    else:
        flash("Could not send the verification email. Check BREVO_API_KEY / EMAIL_FROM "
              "in your .env (see server console for the exact API error).", "warning")
    return redirect(url_for("verify_email"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = sanitize(request.form.get("username"), 20)
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"

        if is_locked_out(username):
            flash(f"Too many failed attempts. Please try again in {LOCKOUT_MINUTES} minutes.", "danger")
            return render_template("login.html")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            if not user["email_verified"]:
                record_attempt(username, True)
                session["_pending_verify_user_id"] = user["id"]
                flash("Please verify your email before logging in.", "danger")
                return redirect(url_for("verify_email"))

            record_attempt(username, True)
            session.clear()
            session["user_id"] = user["id"]
            session["_timeout_minutes"] = user["session_timeout"] or 30
            session["_last_seen"] = datetime.now().isoformat()
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30) if remember else timedelta(minutes=user["session_timeout"] or 30)
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user["id"]))
            db.commit()
            flash(f"Welcome back, {user['full_name'].split(' ')[0]}!", "success")
            return redirect(url_for("dashboard"))
        else:
            record_attempt(username, False)
            flash("Invalid username or password.", "danger")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        flash("If an account with that username exists, recovery instructions would be sent by your administrator. This offline demo does not send email.", "info")
        return redirect(url_for("login"))
    return render_template("forgot.html")


# =====================================================================================
# SECTION 8 — DASHBOARD / HOME
# =====================================================================================
@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    stats = get_stats(session["user_id"])
    recent = db.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC, id DESC LIMIT 5",
        (session["user_id"],)).fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent, active="home", notif_count=1 if stats["budget_pct"] >= 80 else 0)


# =====================================================================================
# SECTION 9 — ALLOWANCE
# =====================================================================================
@app.route("/allowance/add", methods=["GET", "POST"])
@login_required
def add_allowance():
    if request.method == "POST":
        amount = parse_amount(request.form.get("amount"))
        source = sanitize(request.form.get("source"), 60)
        date = parse_date(request.form.get("date"))
        notes = sanitize(request.form.get("notes"), 300)
        recurring = request.form.get("recurring", "none")
        if recurring not in ("none", "daily", "weekly", "monthly"):
            recurring = "none"

        if not amount or not source:
            flash("Please enter a valid amount and source.", "danger")
            return render_template("add_allowance.html", today=datetime.now().strftime("%Y-%m-%d"), active="home")

        db = get_db()
        db.execute(
            "INSERT INTO transactions (user_id, type, amount, category, date, notes, recurring, created_at) "
            "VALUES (?, 'allowance', ?, ?, ?, ?, ?, ?)",
            (session["user_id"], amount, source, date, notes, recurring, datetime.now().isoformat())
        )
        db.commit()
        flash(f"Allowance of ₱{amount:.2f} added successfully!", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_allowance.html", today=datetime.now().strftime("%Y-%m-%d"), active="home")


# =====================================================================================
# SECTION 10 — EXPENSES
# =====================================================================================
@app.route("/expense/add", methods=["GET", "POST"])
@login_required
def add_expense():
    if request.method == "POST":
        amount = parse_amount(request.form.get("amount"))
        category = request.form.get("category") if request.form.get("category") in CATEGORIES else "Others"
        date = parse_date(request.form.get("date"))
        notes = sanitize(request.form.get("notes"), 300)
        receipt = encode_upload(request.files.get("receipt"))

        if not amount:
            flash("Please enter a valid amount.", "danger")
            return render_template("add_expense.html", today=datetime.now().strftime("%Y-%m-%d"), active="home")

        db = get_db()
        db.execute(
            "INSERT INTO transactions (user_id, type, amount, category, date, notes, receipt, created_at) "
            "VALUES (?, 'expense', ?, ?, ?, ?, ?, ?)",
            (session["user_id"], amount, category, date, notes, receipt, datetime.now().isoformat())
        )
        db.commit()
        flash(f"Expense of ₱{amount:.2f} recorded.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_expense.html", today=datetime.now().strftime("%Y-%m-%d"), active="home")


# =====================================================================================
# SECTION 11 — TRANSACTIONS (search / filter / sort / edit / delete / pagination / export)
# =====================================================================================
@app.route("/transactions")
@login_required
def transactions():
    db = get_db()
    q = sanitize(request.args.get("q", ""), 100)
    type_f = request.args.get("type", "")
    sort = request.args.get("sort", "date_desc")
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 10

    sql = "SELECT * FROM transactions WHERE user_id = ?"
    params = [session["user_id"]]
    if q:
        sql += " AND (category LIKE ? OR notes LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if type_f in ("allowance", "expense", "savings"):
        sql += " AND type = ?"
        params.append(type_f)

    order_map = {
        "date_desc": "date DESC, id DESC", "date_asc": "date ASC, id ASC",
        "amount_desc": "amount DESC", "amount_asc": "amount ASC",
    }
    sql += " ORDER BY " + order_map.get(sort, "date DESC, id DESC")

    all_rows = db.execute(sql, params).fetchall()
    total_pages = max((len(all_rows) + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    rows = all_rows[(page - 1) * per_page: page * per_page]

    return render_template("transactions.html", rows=rows, q=q, type_f=type_f, sort=sort,
                            page=page, total_pages=total_pages, active="transactions")


@app.route("/transactions/<int:tx_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(tx_id):
    db = get_db()
    t = db.execute("SELECT * FROM transactions WHERE id=? AND user_id=?", (tx_id, session["user_id"])).fetchone()
    if not t:
        flash("Transaction not found.", "danger")
        return redirect(url_for("transactions"))

    if request.method == "POST":
        amount = parse_amount(request.form.get("amount"))
        category = sanitize(request.form.get("category"), 60)
        date = parse_date(request.form.get("date"))
        notes = sanitize(request.form.get("notes"), 300)
        if not amount:
            flash("Please enter a valid amount.", "danger")
        else:
            db.execute("UPDATE transactions SET amount=?, category=?, date=?, notes=? WHERE id=? AND user_id=?",
                       (amount, category, date, notes, tx_id, session["user_id"]))
            db.commit()
            flash("Transaction updated.", "success")
            return redirect(url_for("transactions"))

    return render_template("edit_tx.html", t=t, active="transactions")


@app.route("/transactions/<int:tx_id>/delete", methods=["POST"])
@login_required
def delete_transaction(tx_id):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (tx_id, session["user_id"]))
    db.commit()
    flash("Transaction deleted.", "info")
    return redirect(url_for("transactions"))


@app.route("/export/csv")
@login_required
def export_csv():
    db = get_db()
    rows = db.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC", (session["user_id"],)).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Type", "Category/Source", "Notes", "Amount"])
    for t in rows:
        writer.writerow([t["date"], t["type"], t["category"] or "", t["notes"] or "", f"{t['amount']:.2f}"])
    output = buf.getvalue()
    return Response(output, mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=transactions_export.csv"})


@app.route("/export/print")
@login_required
def export_print():
    db = get_db()
    rows = db.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC", (session["user_id"],)).fetchall()
    stats = get_stats(session["user_id"])
    return render_template("print.html", rows=rows, stats=stats, now=datetime.now().strftime("%Y-%m-%d %H:%M"))


# =====================================================================================
# SECTION 12 — BUDGET
# =====================================================================================
@app.route("/budget", methods=["GET", "POST"])
@login_required
def budget():
    db = get_db()

    if request.method == "POST":
        budget_type = request.form.get("budget_type", "monthly")
        if budget_type not in BUDGET_TYPES:
            budget_type = "monthly"
        amount = parse_amount(request.form.get("budget_amount")) or 0
        column = f"{budget_type}_budget"  # column name comes only from the validated BUDGET_TYPES whitelist
        db.execute(f"UPDATE users SET {column}=? WHERE id=?", (amount, session["user_id"]))
        db.commit()
        flash(f"{budget_type.capitalize()} budget updated successfully.", "success")
        return redirect(url_for("budget", type=budget_type))

    budget_type = request.args.get("type", "monthly")
    if budget_type not in BUDGET_TYPES:
        budget_type = "monthly"
    bstats = get_budget_stats(session["user_id"], budget_type)
    return render_template("budget.html", bstats=bstats, active="budget")


# =====================================================================================
# SECTION 13 — SAVINGS
# =====================================================================================
@app.route("/savings")
@login_required
def savings():
    db = get_db()
    goals = db.execute("SELECT * FROM savings_goals WHERE user_id=? ORDER BY created_at DESC", (session["user_id"],)).fetchall()
    return render_template("savings.html", goals=goals, active="savings")


@app.route("/savings/goal/add", methods=["POST"])
@login_required
def add_goal():
    db = get_db()
    goal_name = sanitize(request.form.get("goal_name"), 60)
    goal_amount = parse_amount(request.form.get("goal_amount"))
    deadline = request.form.get("deadline") or None

    if not goal_name or not goal_amount:
        flash("Please provide a valid goal name and target amount.", "danger")
    else:
        db.execute(
            "INSERT INTO savings_goals (user_id, goal_name, goal_amount, current_saved, deadline, created_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (session["user_id"], goal_name, goal_amount, deadline, datetime.now().isoformat())
        )
        db.commit()
        flash("Savings goal created!", "success")
    return redirect(url_for("savings"))


@app.route("/savings/transfer", methods=["POST"])
@login_required
def add_savings_tx():
    db = get_db()
    amount = parse_amount(request.form.get("amount"))
    goal_id = request.form.get("goal_id") or None

    if not amount:
        flash("Please enter a valid amount to transfer.", "danger")
        return redirect(url_for("savings"))

    db.execute(
        "INSERT INTO transactions (user_id, type, amount, category, date, notes, goal_id, created_at) "
        "VALUES (?, 'savings', ?, ?, ?, '', ?, ?)",
        (session["user_id"], amount, "Savings Transfer", datetime.now().strftime("%Y-%m-%d"), goal_id, datetime.now().isoformat())
    )
    if goal_id:
        db.execute("UPDATE savings_goals SET current_saved = current_saved + ? WHERE id=? AND user_id=?",
                   (amount, goal_id, session["user_id"]))
    db.commit()
    flash(f"₱{amount:.2f} transferred to savings!", "success")
    return redirect(url_for("savings"))


# =====================================================================================
# SECTION 14 — REPORTS / ANALYTICS
# =====================================================================================
@app.route("/reports")
@login_required
def reports():
    cd = chart_data(session["user_id"])
    return render_template("reports.html", cd=cd, year=datetime.now().year, active="reports")


# =====================================================================================
# SECTION 15 — SETTINGS
# =====================================================================================
@app.route("/settings")
@login_required
def settings():
    stats = get_stats(session["user_id"])
    return render_template("settings.html", stats=stats, active="settings")


@app.route("/settings/profile", methods=["POST"])
@login_required
def update_profile():
    db = get_db()
    full_name = sanitize(request.form.get("full_name"), 80)
    username = sanitize(request.form.get("username"), 20)
    bio = sanitize(request.form.get("bio"), 150)

    if not full_name or not valid_username(username):
        flash("Please provide a valid name and username.", "danger")
        return redirect(url_for("settings"))

    dupe = db.execute("SELECT id FROM users WHERE username=? AND id != ?", (username, session["user_id"])).fetchone()
    if dupe:
        flash("That username is already taken.", "danger")
        return redirect(url_for("settings"))

    db.execute("UPDATE users SET full_name=?, username=?, bio=? WHERE id=?", (full_name, username, bio, session["user_id"]))
    db.commit()
    flash("Profile updated successfully.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/picture", methods=["POST"])
@login_required
def update_picture():
    db = get_db()
    if request.form.get("remove") == "1":
        db.execute("UPDATE users SET profile_pic=NULL WHERE id=?", (session["user_id"],))
        db.commit()
        flash("Profile picture removed.", "info")
        return redirect(url_for("settings"))

    encoded = encode_upload(request.files.get("profile_pic"))
    if not encoded:
        flash("Please upload a valid image (png/jpg/jpeg/gif/webp, max 4MB).", "danger")
        return redirect(url_for("settings"))

    db.execute("UPDATE users SET profile_pic=? WHERE id=?", (encoded, session["user_id"]))
    db.commit()
    flash("Profile picture updated.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/cover-photo", methods=["POST"])
@login_required
def update_cover_photo():
    db = get_db()
    if request.form.get("remove") == "1":
        db.execute("UPDATE users SET cover_photo=NULL WHERE id=?", (session["user_id"],))
        db.commit()
        flash("Cover photo removed.", "info")
        return redirect(url_for("settings"))

    encoded = encode_upload(request.files.get("cover_photo"))
    if not encoded:
        flash("Please upload a valid image (png/jpg/jpeg/gif/webp, max 4MB).", "danger")
        return redirect(url_for("settings"))

    db.execute("UPDATE users SET cover_photo=? WHERE id=?", (encoded, session["user_id"]))
    db.commit()
    flash("Cover photo updated.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    current_pw = request.form.get("current_password") or ""
    new_pw = request.form.get("new_password") or ""
    confirm_pw = request.form.get("confirm_password") or ""

    if not check_password_hash(user["password_hash"], current_pw):
        flash("Current password is incorrect.", "danger")
    elif new_pw != confirm_pw:
        flash("New passwords do not match.", "danger")
    else:
        score, missing = password_strength(new_pw)
        if score < 5:
            flash("New password does not meet strength requirements.", "danger")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), session["user_id"]))
            db.commit()
            flash("Password changed successfully.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/theme", methods=["POST"])
@login_required
def update_theme():
    db = get_db()
    theme = request.form.get("theme", "blue")
    if theme not in ("light", "dark", "blue", "system"):
        theme = "blue"
    db.execute("UPDATE users SET theme=? WHERE id=?", (theme, session["user_id"]))
    db.commit()
    flash("Theme updated.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/notifications", methods=["POST"])
@login_required
def update_notifications():
    db = get_db()
    notif = 1 if request.form.get("notifications_enabled") == "on" else 0
    budget_alerts = 1 if request.form.get("budget_alerts") == "on" else 0
    savings_alerts = 1 if request.form.get("savings_alerts") == "on" else 0
    db.execute("UPDATE users SET notifications_enabled=?, budget_alerts=?, savings_alerts=? WHERE id=?",
               (notif, budget_alerts, savings_alerts, session["user_id"]))
    db.commit()
    flash("Notification preferences saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/session-timeout", methods=["POST"])
@login_required
def update_session_timeout():
    db = get_db()
    try:
        timeout = int(request.form.get("session_timeout", 30))
    except ValueError:
        timeout = 30
    if timeout not in (15, 30, 60, 240):
        timeout = 30
    db.execute("UPDATE users SET session_timeout=? WHERE id=?", (timeout, session["user_id"]))
    db.commit()
    session["_timeout_minutes"] = timeout
    flash("Session timeout updated.", "success")
    return redirect(url_for("settings"))


# =====================================================================================
# SECTION 16 — ERROR HANDLERS
# =====================================================================================
@app.errorhandler(404)
def not_found(e):
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.errorhandler(413)
def too_large(e):
    flash("File is too large. Please upload a smaller image (max 6 MB).", "danger")
    return redirect(request.referrer or url_for("dashboard"))


# =====================================================================================
# SECTION 17 — ENTRY POINT
# =====================================================================================
# init_db() runs at import time (not just under __main__) so it also executes when
# gunicorn imports this file on Render, e.g. `gunicorn main:app`.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  {APP_NAME} — {APP_TAGLINE}")
    print(f"  Database ready at: {DB_PATH}")
    print(f"  Starting server on http://127.0.0.1:{port}  (Ctrl+C to stop)\n")
    app.run(host="0.0.0.0", port=port, debug=False)
