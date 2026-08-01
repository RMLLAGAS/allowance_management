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
            flash("Security check failed (your session may have expired). Please try again.", "danger")
            # Prefer sending the user back to the exact page they were on. request.referrer
            # (the Referer header) is unreliable — many browsers/WebViews omit it — which was
            # previously bouncing users all the way to /login and wiping in-progress forms like
            # /register. request.path is more reliable, but only safe to reuse when that same
            # path also accepts GET (most POST-only action endpoints, e.g. /settings/profile,
            # don't and would 405). Fall back to referrer, then to a sensible default page.
            if request.url_rule and "GET" in request.url_rule.methods:
                target = request.path
            elif request.referrer:
                target = request.referrer
            else:
                target = url_for("dashboard") if "user_id" in session else url_for("login")
            return redirect(target)


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
    "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"
)
_ICON_512_B64 = (
    "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"
)
_ICON_APPLE_TOUCH_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAAHrElEQVR4nO3dTWwUZRzH8WemuyxtgbZA1VIKJhZJUUk8qNQIKDECJh41Ykj0oELBxBvRKJpI1BhPRolQEhWjMeHuy0FeSqJ3q+FiJfQFFlv6svTFbtvd9bCwmW55urMzzzMz+/T7SQ+y3Z0ZeX78n/8+M7NrNR3pFsCd2GEfAKKLcEAqZllW2MeAiKJyQIpwQIpwQComaDkgEbNIBySYViDFtAIpKgekYhQOyFA5IBUTLJ9DgsoBKXoOSFE5IMU6B6RignRAgp4DUvQckKLngBSVA1I0pJCiIYUUPQek6DkgRTggxR1vkKJyQIpwQIpwQIpwQIqGFFJUDkgRDkixfA4pTrxBilP2kKLngBQ9B6SoHJCiIYUUDSmk6DkgRc8BqRB6jotvbAx8n4bY8UVvkLuzWo/9HcyeLh4mE8rsOB5ESoIIB7HQRHdEbCEsrT8kQ5+LhzdqHTtr07EeTYfedXiDpi2jyM7jfTo2q+vdCskIkqa/bdY5TKFhHLVUjq5DlI2g6fg7V9+QkoywdB3aoHYoY1x8HnFNPVsX+W2ydd7XAncdannyy35Vu7bu//AfVdsSQlzoaFG4taVs8UwsVJQSJRGJ+d8EFCo3EwtfmE/JhQ4FJYQTbxHiORl33MiFjhafhdza/NFl/weUd/7gelWbWmqUxKJIYaJ56sSAty1QOcKnIxnOzXr+R0s4QqYpGUUb95YPwhEmrcko2oWHfNgqV01QjgCSUbyjMgfUtoSl6ieY/1UzBJYM5+7OH1hf1oAyrYQg4GQ4d3ruQLP7l7AIFkVzyzZN1r0o+WXOzk7amdF4+lJ8+g8rl9Z3GJyyD5qbspGJr5uqe77k0+zszZrUmRUjJ11GpKlna7K1+9yB5l2dV90836YfrVxZe9VEw6vD67/L2SvKeqHLAVV7yh4l6Og2ZhObx+7+uKwDOPt6s6tT9sqPFTpUT/wicjNCiJy9cjbRlond4/ztdO3O2cSD8fRfandKzxE5/618bnb5A0UPxqe7rexU/r8TU79N1u2bW3af8wnjazqWT5yNz/TEp7uFGy7GnbvsIyfV+PbCHuLm2iOLvypdsz1ds7127HuX4XAz7kwrwXHZcKwd2D9T/Uiq8R3ng2sGXraz485HUo1vzVQ/Wvhj1dzg6msH7cyYm8NItnb/+tq6p09dW/yZMcF1ghEzWfdSZlnxnWBT9fvyPUdBJj7vWt2sXTvR8Epi6vfq8Z/c7qnU0FM5Iidduz1r1xc9OF3zxLw/W/GclXA+kLMS07W7quaGFB4JPUfk3HVld7p250jT584HG/tfsDIpIUSuauVMYuv4mjcz8XkL4atufFqb+qGsHZUceipHZRjcuNhkYeWmq8d/VL5TleFoaoi7eVpydFbhTiGEqBt8387eVL7ZEBpSlxkyib5/D1YuvWrok+rxnz29mIbUTNnYzOXlk101qTNVc0lN+6AhrQxrB/Zbt9Y5clZ2ys6k/J+sd9GQkg79mhriydHZZGu35xNvVbN9bha43MjfsvDMV9dLDj1XgkGKcECKTzBeskqPu63s2nMytqj8G/iie+GDlz+A3V//62ZAmVYgRTiCE3rxKJQNl88vYxFsMllyo9xlr0ZisiuUe1uKzPvS4Ynk9cWeS1fhm/8FD8/yZWPPN4Puv2Y6ViIQUC2UfBSSUdar6DlCE1jz4XlH3GUfgsJ56QDyUdjFntODHu6yJxshCCYfhY3vPT3oYUC54y00uvPhSMaQtwHleo4w5ZtTcXsgVbWozrTtPe39kmPCEbJbK2O3I+I/H/MLhi9c7BMJRSVE+P4E42e/HfJ/tovKERXOEiJcp2Rhv9LUEH/4sxK3srnEKftoKYqIKKdddVy5rWZMuUwwipwX6C9+5fqdL+VXNKZMK1EX4p0chMNAqiYDzq1Aip7DRIqu2aRyQIpFMAPRc0A73q2YSN06BxOLeWhIoRkNqYFUjSnrHCZSNKZMK5CiITWSqlP2MI66ngPmoeeAboQDUryVNZGq5XOyYR5VXwDNtAIpwgEpeg4T0XNARuEiGPEwT/Qa0rZ3Q/4EVgghHjtxQ9WmWD43jrqZgHCYRmGXoPitLDNLuLadVDanCBpS46gcTaUfNWkJYYm2o38qPD64t61zWO1QalkhJR/B29Y5rHybKj+H1PmzhXwEqL1zWMcgajy3Qj6C0a6hZuSp7zmcP1veIx96tZ8aUfZNWwt+tJ+VJR/6tJ8a0bp9q+1oQCsTlz54KJgdLQWPa45FXnDhKCAlnuXLcH1LczC7CyEc8K9+QxD5YIW0Io31XQsgH1wmWKnG+q7q3kWMLwuuXLrHjspRwXQXD8JR2bTmg4t9DKBrdqHnqHip/oH6DVq+C5xpBVKEwwRjfQM6Nks4DKEjH6yQmsNS3T9SOcwx2qu4eBAOSBEOo6gtHqxzmEbhgFI5TKOwePBuxUhqxpTKYSBVxYOPfTKUimHlY5/MpGRYmVbMpGRm4XoOg/ktH1QOY4329kfxIxhgBqYVk/mcV6gcJhu50u/n5THtNz8gXD7Gl8oBKRbBDOdnfFk+N9xIb//qe1u8vZZpBVKcsl8KPA4xPYf5PA8x04r5Rq54XEcnHJAiHJDi3MqS4O2bZlnnWBo8jfL/AjXVHwQcXWQAAAAASUVORK5CYII="
)

_SCREENSHOT_LOGIN_B64 = (
    "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"
)
_SCREENSHOT_REGISTER_B64 = (
    "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"
)
_SCREENSHOT_DASHBOARD_B64 = (
    "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"
)
_SCREENSHOT_REPORTS_B64 = (
    "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"
)
_SCREENSHOT_SETTINGS_B64 = (
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
        "display_override": ["standalone", "minimal-ui", "browser"],
        "categories": ["finance", "productivity", "lifestyle"],
        "prefer_related_applications": False,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        "screenshots": [
            {"src": "/screenshot-login.jpg", "sizes": "720x1419", "type": "image/jpeg", "form_factor": "narrow", "label": "Sign in screen"},
            {"src": "/screenshot-register.jpg", "sizes": "720x1528", "type": "image/jpeg", "form_factor": "narrow", "label": "Create account screen"},
            {"src": "/screenshot-dashboard.jpg", "sizes": "720x1424", "type": "image/jpeg", "form_factor": "narrow", "label": "Dashboard overview"},
            {"src": "/screenshot-reports.jpg", "sizes": "720x1417", "type": "image/jpeg", "form_factor": "narrow", "label": "Reports and analytics"},
            {"src": "/screenshot-settings.jpg", "sizes": "720x1514", "type": "image/jpeg", "form_factor": "narrow", "label": "Profile and settings"},
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


_SCREENSHOTS = {
    "login": _SCREENSHOT_LOGIN_B64,
    "register": _SCREENSHOT_REGISTER_B64,
    "dashboard": _SCREENSHOT_DASHBOARD_B64,
    "reports": _SCREENSHOT_REPORTS_B64,
    "settings": _SCREENSHOT_SETTINGS_B64,
}


@app.route("/screenshot-<name>.jpg")
def screenshot(name):
    import base64
    b64 = _SCREENSHOTS.get(name)
    if not b64:
        return Response(status=404)
    return Response(base64.b64decode(b64), mimetype="image/jpeg")


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
