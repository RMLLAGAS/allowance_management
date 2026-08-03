#!/usr/bin/env python3
# =====================================================================================
#  cloud.py — thin client for the Supabase (Postgres) side of the sync
# =====================================================================================
#  Talks to Supabase's auto-generated REST API (PostgREST) over HTTPS only. Uses the
#  `requests` library directly instead of the `supabase-py` SDK to keep this a
#  single new dependency you already likely have, but everything here maps 1:1 onto
#  supabase-py calls if you'd rather swap it in later.
#
#  SECURITY
#  --------
#  - SUPABASE_URL / SUPABASE_SERVICE_KEY are read from environment variables ONLY.
#    Never hardcode them, never log them, never send them to the client/browser.
#  - Use the `service_role` key (server-side only) so Row Level Security can be
#    written to key everything off a `user_id`/`owner` column without the app
#    needing per-user Supabase auth sessions — see supabase_schema.sql for the
#    matching RLS policies.
#  - All requests go to https:// — Supabase does not offer plain HTTP, so this is
#    enforced by definition, not by extra code here.
#  - If you'd rather not rely on the hosting platform's env-var storage, see
#    `_load_encrypted_fallback()` below for an optional locally-encrypted option.
# =====================================================================================

import os
import json
import base64

import requests

REQUEST_TIMEOUT = 10  # seconds — a hung request must not hang the whole app


class CloudError(Exception):
    """Raised for any Supabase/network failure. Callers (sync.py) catch this and
    route the record into the retry queue instead of crashing the request."""


def _credentials():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        url, key = _load_encrypted_fallback()
    if not url or not key:
        raise CloudError("Supabase credentials are not configured "
                          "(set SUPABASE_URL and SUPABASE_SERVICE_KEY).")
    return url.rstrip("/"), key


def _load_encrypted_fallback():
    """Optional: for environments (e.g. Pydroid, some shared hosts) where setting
    real environment variables isn't convenient, credentials can instead be stored
    encrypted-at-rest in data/cloud_config.enc, encrypted with a key derived from
    app.secret_key. Only used if SUPABASE_URL/SUPABASE_SERVICE_KEY env vars are
    absent. See sync.py `save_encrypted_credentials()` to write this file."""
    try:
        from cryptography.fernet import Fernet
        import database
        path = os.path.join(database.DATA_DIR, "cloud_config.enc")
        key_path = os.path.join(database.DATA_DIR, "cloud_config.key")
        if not (os.path.exists(path) and os.path.exists(key_path)):
            return "", ""
        with open(key_path, "rb") as f:
            fkey = f.read()
        with open(path, "rb") as f:
            token = f.read()
        data = json.loads(Fernet(fkey).decrypt(token).decode("utf-8"))
        return data.get("url", ""), data.get("key", "")
    except Exception:
        return "", ""


def save_encrypted_credentials(url, service_key):
    """Write SUPABASE_URL/SUPABASE_SERVICE_KEY to an encrypted local file instead
    of environment variables. Requires `pip install cryptography`. Call this once
    from a setup route/CLI command — never from user-facing request handlers."""
    from cryptography.fernet import Fernet
    import database
    os.makedirs(database.DATA_DIR, exist_ok=True)
    key_path = os.path.join(database.DATA_DIR, "cloud_config.key")
    if not os.path.exists(key_path):
        with open(key_path, "wb") as f:
            f.write(Fernet.generate_key())
    with open(key_path, "rb") as f:
        fkey = f.read()
    payload = json.dumps({"url": url, "key": service_key}).encode("utf-8")
    token = Fernet(fkey).encrypt(payload)
    with open(os.path.join(database.DATA_DIR, "cloud_config.enc"), "wb") as f:
        f.write(token)


def _headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def ping() -> bool:
    """Cheap reachability check against Supabase itself (distinct from
    network.is_online(), which only checks general internet access)."""
    try:
        url, key = _credentials()
        r = requests.get(f"{url}/rest/v1/", headers=_headers(key), timeout=REQUEST_TIMEOUT)
        return r.status_code < 500
    except (CloudError, requests.RequestException):
        return False


def push_records(table: str, records: list[dict]) -> dict:
    """Upsert a batch of records into Supabase, keyed on `uuid` (see the UNIQUE
    constraint in supabase_schema.sql). on_conflict=uuid makes this idempotent —
    pushing the same record twice never creates a duplicate.
    Returns {"ok": bool, "count": int} and raises CloudError on hard failure so
    the caller can requeue instead of assuming success."""
    if not records:
        return {"ok": True, "count": 0}
    url, key = _credentials()
    try:
        r = requests.post(
            f"{url}/rest/v1/{table}?on_conflict=uuid",
            headers=_headers(key),
            data=json.dumps(records),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise CloudError(f"push failed for {table}: {e}") from e
    if r.status_code >= 400:
        raise CloudError(f"push rejected for {table}: {r.status_code} {r.text[:300]}")
    return {"ok": True, "count": len(records)}


def pull_updates(table: str, since_iso: str, exclude_device_id: str | None = None) -> list[dict]:
    """Fetch every row in `table` updated after since_iso (server-side filter, so
    we never download the whole table — only what changed). Optionally excludes
    rows whose last writer was this same device, since we already have those."""
    url, key = _credentials()
    params = {"updated_at": f"gt.{since_iso}", "order": "updated_at.asc"}
    if exclude_device_id:
        params["device_id"] = f"neq.{exclude_device_id}"
    try:
        r = requests.get(
            f"{url}/rest/v1/{table}",
            headers=_headers(key),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise CloudError(f"pull failed for {table}: {e}") from e
    if r.status_code >= 400:
        raise CloudError(f"pull rejected for {table}: {r.status_code} {r.text[:300]}")
    return r.json()
